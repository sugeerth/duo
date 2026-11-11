"""Loopback live event stream; metadata only, no response/trace files.

Events form a tree: session -> turn -> agent run -> tool. A span is opened with
HUB.span(...); every event emitted inside it carries span_id, so the viewer can
nest tools under the agent run and agent runs under the turn. Spans are tracked
per thread; HUB.bind(parent, fn) carries a parent into a worker thread.
"""
from collections import deque
import contextlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs
import uuid

VIEWER=Path(__file__).with_name('viewer.html')
D3=Path(__file__).with_name('d3.v7.min.js')  # vendored so the viewer works offline
RING=5000  # long sessions: keep enough metadata for a multi-hour run

class Span:
    def __init__(self,span_id,parent_id,kind,provider):
        self.id=span_id;self.parent_id=parent_id;self.kind=kind;self.provider=provider
        self.status='completed';self.tools=0;self.tool_errors=0;self.approvals=0;self.declined=0

class TraceHub:
    def __init__(self):
        self.events=deque(maxlen=RING);self.condition=threading.Condition();self.sequence=0;self.server=None
        self.session_id=str(uuid.uuid4());self.started=time.time()
        self._local=threading.local();self._spans={}
        self.routes={}  # extra JSON GET endpoints registered by the host (e.g. /learning); fn(query_dict)
        self.post_routes={}  # JSON POST endpoints (viewer controls); fn(body_dict)
        self.store=None;self.payloads={};self._payload_order=deque(maxlen=2000)  # recent payloads for on-demand fetch
    def attach_store(self,store):
        """Persist every event (and payloads) to a TraceStore; None disables."""
        self.store=store
        if store:
            import atexit;atexit.register(store.flush)
        if store:store.session(self.session_id,self.started,str(Path.cwd()))
    # --- span tree ---
    def _stack(self):
        if not hasattr(self._local,'stack'):self._local.stack=[]
        return self._local.stack
    def current(self):
        stack=self._stack();return stack[-1] if stack else None
    @contextlib.contextmanager
    def span(self,kind,provider='duo',**fields):
        parent=self.current()
        span=Span(uuid.uuid4().hex[:12],parent.id if parent else None,kind,provider)
        with self.condition:self._spans[span.id]=span
        started=time.monotonic()
        self.emit('span.start',provider,span_id=span.id,parent_id=span.parent_id,span_kind=kind,**fields)
        self._stack().append(span)
        try:yield span
        except BaseException:
            span.status='failed';raise
        finally:
            self._stack().pop()
            self.emit('span.end',provider,span_id=span.id,parent_id=span.parent_id,span_kind=kind,status=span.status,
                duration_ms=int((time.monotonic()-started)*1000),tools=span.tools,tool_errors=span.tool_errors,
                approvals=span.approvals,declined=span.declined,**fields)
            with self.condition:self._spans.pop(span.id,None)
    def bind(self,parent,fn):
        """Run fn in another thread as a child of parent (a Span or None)."""
        def bound(*args,**kwargs):
            stack=self._stack();saved=list(stack);stack[:]=[parent] if parent else []
            try:return fn(*args,**kwargs)
            finally:stack[:]=saved
        return bound
    # --- events ---
    def emit(self,kind,provider='duo',payload=None,payload_kind=None,**fields):
        # The live stream carries metadata only. Heavy text (reasoning, tool I/O, answers) is a payload:
        # kept in a bounded memory cache + the trace DB, and fetched by the viewer on demand.
        allowed={'status','tool','method','duration_ms','name','passed','phase','count','request_id','turn_id','item_id','reason_code',
                 'span_id','parent_id','span_kind','role','mode','round','verdict','findings','score','tools','tool_errors','approvals','declined',
                 'judge','tags','rationale','confidence','detail','chars','payload_kind','data','model'}  # judge.py caps rationale length
        if 'detail' in fields:fields['detail']=str(fields['detail'])[:100]
        event={'id':None,'timestamp':time.time(),'session_id':self.session_id,'provider':provider,'type':kind}
        event.update({k:v for k,v in fields.items() if k in allowed})
        span=self.current()
        if span and 'span_id' not in event:event['span_id']=span.id
        if span:self._count(span,kind,event)
        if payload:
            payload=str(payload);event['chars']=len(payload);event['payload_kind']=payload_kind or kind
        with self.condition:
            self.sequence+=1;event['id']=self.sequence;self.events.append(event)
            if payload:
                if len(self._payload_order)==self._payload_order.maxlen:self.payloads.pop(self._payload_order[0],None)
                self._payload_order.append(event['id']);self.payloads[event['id']]=(event['payload_kind'],payload[:20000],event.get('span_id'),provider)
            self.condition.notify_all()
        if self.store:self.store.event(event,payload,event.get('payload_kind'))
        return event
    def payload(self,event_id,session=None):
        if session and session!=self.session_id:return self.store.payload(session,event_id) if self.store else None
        with self.condition:item=self.payloads.get(event_id)
        if item:return {'kind':item[0],'text':item[1],'span_id':item[2],'provider':item[3]}
        return self.store.payload(self.session_id,event_id) if self.store else None
    def span_payloads(self,span_id,session=None):
        session=session or self.session_id
        if self.store:
            self.store.flush(2)
            return self.store.payloads_for_span(session,span_id)
        with self.condition:return [{'event_id':i,'kind':k,'provider':p,'text':t} for i,(k,t,sp,p) in self.payloads.items() if sp==span_id]
    def _count(self,span,kind,event):
        if kind=='tool.started':span.tools+=1
        elif kind=='tool.completed' and event.get('passed') is False:span.tool_errors+=1
        elif kind=='approval.requested':span.approvals+=1
        elif kind=='approval.resolved' and event.get('status') in ('declined','decline'):span.declined+=1
    # --- loopback server ---
    def start(self,port=0):
        if self.server:return self.server.server_address[1]
        hub=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send_body(self,data,kind):
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
            def send_json(self,obj,status=200):
                data=json.dumps(obj).encode()
                if status!=200:self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
                self.send_body(data,'application/json')
            def do_POST(self):
                path=self.path.split('?',1)[0]
                # Only the duo viewer can call these: a custom header forces a CORS preflight that this server never approves,
                # so other websites cannot post here; and the server only listens on 127.0.0.1.
                if path not in hub.post_routes or self.headers.get('X-Duo')!='1':self.send_json({'error':'forbidden'},403);return
                try:
                    n=int(self.headers.get('Content-Length','0'));body=json.loads(self.rfile.read(min(n,65536)) or b'{}')
                    self.send_json(hub.post_routes[path](body))
                except Exception as exc:self.send_json({'error':str(exc)},400)
            def do_GET(self):
                path,_,query=self.path.partition('?');q={k:v[0] for k,v in parse_qs(query).items()};self.path=path
                session=q.get('session')
                if self.path=='/payload':
                    try:item=hub.payload(int(q.get('id','0')),session)
                    except ValueError:item=None
                    self.send_json(item or {'error':'payload not found (evicted, or the trace DB is off)'},200 if item else 404);return
                if self.path in hub.routes:
                    try:self.send_json(hub.routes[self.path](q))
                    except Exception as exc:self.send_json({'error':str(exc)},500)
                    return
                if self.path=='/span_payloads':self.send_json(hub.span_payloads(q.get('span',''),session));return
                if self.path=='/sessions':
                    stored=hub.store.sessions() if hub.store else []
                    self.send_json({'current':hub.session_id,'db':str(hub.store.path) if hub.store else None,'sessions':stored});return
                if self.path=='/snapshot' and session and session!=hub.session_id:
                    snap=hub.store.snapshot(session) if hub.store else None
                    self.send_json(snap or {'error':'unknown session'},200 if snap else 404);return
                if self.path=='/':
                    try:data=VIEWER.read_bytes()
                    except OSError:data=b'<!doctype html><meta charset="utf-8"><title>Duo traces</title><p>viewer.html missing; use /snapshot or /events.'
                    self.send_body(data,'text/html; charset=utf-8');return
                if self.path=='/d3.v7.min.js':
                    try:self.send_body(D3.read_bytes(),'text/javascript; charset=utf-8')
                    except OSError:self.send_error(404)
                    return
                if self.path=='/snapshot':
                    with hub.condition:data=json.dumps({'session_id':hub.session_id,'started':hub.started,'events':list(hub.events)}).encode()
                    self.send_body(data,'application/json');return
                if self.path=='/events' and session and session!=hub.session_id and hub.store:
                    # stream a session written by another duo process by tailing the trace DB
                    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.end_headers()
                    try:last=int(self.headers.get('Last-Event-ID') or q.get('after') or 0)
                    except ValueError:last=0
                    idle=0
                    try:
                        while True:
                            batch=hub.store.events_since(session,last)
                            for event in batch:
                                self.wfile.write(('id: '+str(event['id'])+'\ndata: '+json.dumps(event)+'\n\n').encode());last=event['id']
                            if batch:self.wfile.flush();idle=0
                            else:
                                idle+=1
                                if idle%10==0:self.wfile.write(b': heartbeat\n\n');self.wfile.flush()
                            time.sleep(0.5 if batch else 1.0)
                    except (BrokenPipeError,ConnectionResetError):pass
                    return
                if self.path!='/events':self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.end_headers()
                try:last=int(self.headers.get('Last-Event-ID','0'))
                except ValueError:last=0
                try:
                    while True:
                        with hub.condition:
                            batch=[event for event in hub.events if event['id']>last]
                            if not batch:hub.condition.wait(timeout=10);batch=[event for event in hub.events if event['id']>last]
                        if not batch:self.wfile.write(b': heartbeat\n\n');self.wfile.flush();continue
                        for event in batch:
                            self.wfile.write(('id: '+str(event['id'])+'\ndata: '+json.dumps(event)+'\n\n').encode());last=event['id']
                        self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',port),Handler);self.server.daemon_threads=True
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        return self.server.server_address[1]
    def close(self):
        if self.server:self.server.shutdown();self.server.server_close();self.server=None

HUB=TraceHub()
def emit(kind,provider='duo',payload=None,payload_kind=None,**fields):return HUB.emit(kind,provider,payload,payload_kind,**fields)
