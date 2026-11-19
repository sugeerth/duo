#!/usr/bin/env python3
"""AgentLens Cloud: live traces from Claude Code and Codex, online, with a free tier and a Pro tier.

    AGENTLENS_CLOUD_URL=https://lens.example.com python3 cloud/server.py --port 8080

How traces arrive: a person signs up on the landing page and gets an API key and ONE install line:

    curl -fsSL https://lens.example.com/install.sh | sh -s -- al_...

It installs `agentlens-send` (cloud/agentlens_send.py), a small watcher that tails the transcripts Claude Code
(~/.claude/projects/**.jsonl) and Codex (~/.codex/sessions/**.jsonl) already write, redacts secrets, and posts new
lines here every ~1.5 s. The server rebuilds that session with the same importer the local viewer uses
(chatimport.py), and every open viewer reloads it: the tree, alarms, security review and long-run reading update live.

Plans (server-enforced; the viewer only draws the locks):
  free  the 3 most recent sessions of the last 7 days; live tree, timeline, alarms, security and long-run panels
  pro   every session and retention; Fleet + catch matrix, long-horizon panel, patterns map, long-term trends
Payments: Stripe Checkout (subscription). Configure STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET; the
webhook (POST /stripe/webhook, signature-verified) flips the plan. AGENTLENS_CLOUD_DEV=1 replaces checkout with an
instant upgrade, for local testing only.

Storage: AGENTLENS_CLOUD_DATA (default ./cloud-data): accounts.db, then one trace DB and the raw transcripts per
account. API keys are stored hashed. Nothing here calls a model.
"""
import argparse
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;sys.path.insert(0,str(ROOT))
os.environ.setdefault('AGENTLENS_TRACE_DB','off')  # this process never writes a local ~/.agentlens DB
import chatimport, fleet, facets, longterm, rundiff, tracestore  # noqa: E402

DATA=Path(os.environ.get('AGENTLENS_CLOUD_DATA') or ROOT/'cloud-data')
PUBLIC=(os.environ.get('AGENTLENS_CLOUD_URL') or os.environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')  # Render sets RENDER_EXTERNAL_URL
DEV=os.environ.get('AGENTLENS_CLOUD_DEV')=='1'
def payments():return DEV or bool(os.environ.get('STRIPE_SECRET_KEY') and os.environ.get('STRIPE_PRICE_ID'))  # without Stripe, Pro shows as coming soon
PRICE_LABEL=os.environ.get('AGENTLENS_PRO_PRICE_LABEL','$12 / month')
FREE_SESSIONS=3;FREE_DAYS=7
PRO_EVENTS={'analysis.horizon','reader.read','reader.ask','fork.step'}
PRO_ROUTES={'/fleet':'fleet','/facets':'patterns','/longterm':'longterm','/diff':'diff'}
LOCKED_FREE=['fleet','patterns','longterm','horizon']
MAX_BODY=4_000_000;MAX_LINES=60_000
REV={};REV_LOCK=threading.Lock();STORES={};STORE_LOCK=threading.Lock();BUILD_LOCKS={}

# ------------------------------------------------------------------ accounts
@contextlib.contextmanager
def adb():
    DATA.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(DATA/'accounts.db',timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, email TEXT, key_hash TEXT UNIQUE, plan TEXT, created REAL, stripe_customer TEXT)')
        yield db;db.commit()
    finally:db.close()
def h(key):return hashlib.sha256(key.encode()).hexdigest()
def signup(email):
    email=str(email or '').strip()[:200]
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise ValueError('enter a valid email address')
    key='al_'+secrets.token_urlsafe(24);aid=secrets.token_hex(8)
    with adb() as db:db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?)',(aid,email,h(key),'free',time.time(),None))
    return aid,key
def account(key):
    if not key or not key.startswith('al_'):return None
    with adb() as db:r=db.execute('SELECT id,email,plan FROM accounts WHERE key_hash=?',(h(key),)).fetchone()
    return {'id':r[0],'email':r[1],'plan':r[2]} if r else None
def set_plan(aid,plan,customer=None):
    with adb() as db:
        db.execute('UPDATE accounts SET plan=? WHERE id=?',(plan,aid))
        if customer:db.execute('UPDATE accounts SET stripe_customer=? WHERE id=?',(customer,aid))
def by_customer(customer):
    with adb() as db:r=db.execute('SELECT id FROM accounts WHERE stripe_customer=?',(customer,)).fetchone()
    return r[0] if r else None

def store(aid):
    with STORE_LOCK:
        if aid not in STORES:STORES[aid]=tracestore.TraceStore(DATA/'acct'/aid/'traces.db')
        return STORES[aid]

# ------------------------------------------------------------------ ingest
def ingest(acct,body):
    """Append a transcript's new lines and rebuild its session. Returns the session id."""
    fid=re.sub(r'[^A-Za-z0-9_-]','',str(body.get('file_id') or ''))[:40]
    lines=[str(l) for l in body.get('lines') or [] if isinstance(l,str)]
    if not fid or not lines:raise ValueError('file_id and lines are required')
    provider=body.get('provider') if body.get('provider') in ('claude','codex') else None
    raw=DATA/'acct'/acct['id']/'raw';raw.mkdir(parents=True,exist_ok=True);f=raw/f'{fid}.jsonl'
    with REV_LOCK:lock=BUILD_LOCKS.setdefault((acct['id'],fid),threading.Lock())
    with lock:
        with (f.open() if f.exists() else contextlib.nullcontext([])) as fh:n=sum(1 for _ in fh)
        if n+len(lines)>MAX_LINES:raise ValueError(f'transcript over {MAX_LINES} lines')
        with f.open('a') as out:out.write(''.join(l.rstrip('\n')+'\n' for l in lines))
        st=store(acct['id']);meta=raw/f'{fid}.sid'
        old=meta.read_text().strip() if meta.exists() else None
        if old:st.delete_session(old)
        sid,turns=chatimport.import_session(st,f,provider)
        if sid:meta.write_text(sid)
        st.flush(10)
    if sid:
        with REV_LOCK:REV[(acct['id'],sid)]=REV.get((acct['id'],sid),0)+1
    fleet._CACHE['data']=None
    return sid,turns

def visible_sessions(acct):
    """Newest first by when this server last received lines for the session (the transcript's own clock can be old)."""
    raw=DATA/'acct'/acct['id']/'raw';recv={}
    for m in raw.glob('*.sid') if raw.is_dir() else []:
        try:recv[m.read_text().strip()]=m.stat().st_mtime
        except OSError:pass
    rows=[s for s in store(acct['id']).sessions(limit=500) if s.get('turns')]
    for r in rows:r['received']=recv.get(r['id'],r.get('last_ts') or 0)
    rows.sort(key=lambda r:-r['received'])
    if acct['plan']=='pro':return rows,0
    fresh=[r for r in rows if r['received']>=time.time()-FREE_DAYS*86400][:FREE_SESSIONS]
    return fresh,len(rows)-len(fresh)

# ------------------------------------------------------------------ Stripe
def stripe_checkout(acct,base):
    key=os.environ.get('STRIPE_SECRET_KEY');price=os.environ.get('STRIPE_PRICE_ID')
    if not key or not price:raise RuntimeError('payments are not configured on this server yet')
    form=urllib.parse.urlencode({'mode':'subscription','line_items[0][price]':price,'line_items[0][quantity]':'1',
        'client_reference_id':acct['id'],'customer_email':acct['email'],'success_url':base+'/?upgraded=1','cancel_url':base+'/'}).encode()
    req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=form,headers={'Authorization':'Bearer '+key})
    with urllib.request.urlopen(req,timeout=20) as r:return json.loads(r.read())['url']

def verify_stripe(payload,header,secret,tolerance=300,now=None):
    """Stripe's scheme: header 't=TS,v1=SIG'; SIG = HMAC-SHA256(secret, f'{TS}.{payload}')."""
    parts=dict(p.split('=',1) for p in (header or '').split(',') if '=' in p)
    try:ts=int(parts.get('t',''))
    except ValueError:return False
    sigs=[v for k,v in (p.split('=',1) for p in header.split(',') if '=' in p) if k=='v1']
    want=hmac.new(secret.encode(),f'{ts}.'.encode()+payload,hashlib.sha256).hexdigest()
    return abs((now or time.time())-ts)<=tolerance and any(hmac.compare_digest(want,s) for s in sigs)

def stripe_event(ev):
    t=ev.get('type');o=(ev.get('data') or {}).get('object') or {}
    if t=='checkout.session.completed' and o.get('client_reference_id'):set_plan(o['client_reference_id'],'pro',o.get('customer'));return 'upgraded'
    if t in ('customer.subscription.deleted','customer.subscription.paused'):
        aid=by_customer(o.get('customer'))
        if aid:set_plan(aid,'free');return 'downgraded'
    return 'ignored'

# ------------------------------------------------------------------ pages
def base_url(handler):
    if PUBLIC:return PUBLIC
    proto=handler.headers.get('X-Forwarded-Proto','http');return f"{proto}://{handler.headers.get('Host','127.0.0.1')}"
def install_line(base,key):return f'curl -fsSL {base}/install.sh | sh -s -- {key}'

def viewer_page(acct,base,key):
    v=(ROOT/'viewer.html').read_text()
    cfg={'plan':acct['plan'],'email':acct['email'],'locked':[] if acct['plan']=='pro' else LOCKED_FREE,'price':PRICE_LABEL,'install':install_line(base,key),'dev':DEV,'payments':payments()}
    inject=f'<script>window.AGENTLENS_PAYWALL={json.dumps(cfg).replace("</","<\\/")};</script>\n'+(HERE/'cloud_shim.html').read_text()
    return v.replace('<script src="/d3.v7.min.js"></script>',inject+'<script src="/d3.v7.min.js"></script>',1)

def landing(base,msg=''):
    rows=[('Live tree of every tool call, as the agent works','✓','✓'),('Alarms: guard, repeats, failure streaks, context size','✓','✓'),
          ('Security review of each tool call (OWASP LLM Top 10)','✓','✓'),('Long-run reading: bursts, loops, stalls','✓','✓'),
          ('Sessions kept',f'{FREE_SESSIONS} most recent, {FREE_DAYS} days','All, no limit'),
          ('Fleet view and catch matrix: which instrument caught each run','—','✓'),('Run diff: any two runs aligned, with verdict, cause, cost and fix','—','✓'),('Long-horizon failure modes and milestones','—','✓'),
          ('Patterns map across runs, long-term trends','—','✓')]
    tr=''.join(f'<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>' for a,b,c in rows)
    pro=PRICE_LABEL if payments() else 'coming soon';note='Upgrade from your dashboard at any time. Payments are handled by Stripe; cancel whenever you like.' if payments() else 'Pro is coming soon. Everything on the free plan works today.'
    return (HERE/'landing.html').read_text().replace('{{ROWS}}',tr).replace('{{PRICE}}',html.escape(pro)).replace('{{PLANNOTE}}',html.escape(note)).replace('{{MSG}}',html.escape(msg)).replace('{{BASE}}',html.escape(base))

# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version='AgentLensCloud/0.1'
    def log_message(self,*a):pass
    def send(self,code,body,kind='application/json',headers=()):
        data=body if isinstance(body,bytes) else (json.dumps(body) if kind=='application/json' else body).encode()
        self.send_response(code);self.send_header('Content-Type',kind+('; charset=utf-8' if kind.startswith('text') else ''));self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        for k,v in headers:self.send_header(k,v)
        self.end_headers();self.wfile.write(data)
    def key(self):
        a=self.headers.get('Authorization','')
        if a.startswith('Bearer '):return a[7:].strip()
        for part in self.headers.get('Cookie','').split(';'):
            k,_,v=part.strip().partition('=')
            if k=='al_key':return urllib.parse.unquote(v)
        return None
    def body(self):
        n=int(self.headers.get('Content-Length') or 0)
        if n>MAX_BODY:raise ValueError('request too large')
        raw=self.rfile.read(n) if n else b''
        return raw
    def cookie(self,key,max_age=31536000):
        secure='; Secure' if base_url(self).startswith('https') else ''
        return ('Set-Cookie',f'al_key={urllib.parse.quote(key)}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}{secure}')

    def do_POST(self):
        path=self.path.split('?',1)[0]
        try:raw=self.body()
        except ValueError as e:return self.send(413,{'error':str(e)})
        if path=='/stripe/webhook':
            secret=os.environ.get('STRIPE_WEBHOOK_SECRET')
            if not secret or not verify_stripe(raw,self.headers.get('Stripe-Signature',''),secret):return self.send(400,{'error':'bad signature'})
            try:return self.send(200,{'result':stripe_event(json.loads(raw))})
            except ValueError:return self.send(400,{'error':'bad json'})
        try:body=json.loads(raw or b'{}')
        except ValueError:return self.send(400,{'error':'bad json'})
        if path=='/api/signup':
            if self.headers.get('X-AgentLens')!='1':return self.send(403,{'error':'forbidden'})
            try:aid,key=signup(body.get('email'))
            except ValueError as e:return self.send(400,{'error':str(e)})
            return self.send(200,{'key':key,'install':install_line(base_url(self),key)},headers=[self.cookie(key)])
        acct=account(self.key())
        if not acct:return self.send(401,{'error':'sign in or pass your API key'})
        if path=='/v1/transcripts':
            try:sid,turns=ingest(acct,body)
            except ValueError as e:return self.send(400,{'error':str(e)})
            return self.send(200,{'session':sid,'turns':turns})
        if path=='/api/checkout':
            if self.headers.get('X-AgentLens')!='1':return self.send(403,{'error':'forbidden'})
            if DEV:set_plan(acct['id'],'pro');return self.send(200,{'url':'/?upgraded=1','dev':True})
            try:return self.send(200,{'url':stripe_checkout(acct,base_url(self))})
            except Exception as e:return self.send(503,{'error':str(e)})
        return self.send(404,{'error':'not found'})

    def do_GET(self):
        path,_,query=self.path.partition('?');q={k:v[0] for k,v in urllib.parse.parse_qs(query).items()}
        if path=='/healthz':return self.send(200,{'ok':True})
        if path=='/install.sh':return self.send(200,(HERE/'install.sh').read_text().replace('__AGENTLENS_URL__',base_url(self)),'text/plain')
        if path=='/agentlens-send.py':return self.send(200,(HERE/'agentlens_send.py').read_bytes(),'text/x-python')
        if path=='/d3.v7.min.js':return self.send(200,(ROOT/'d3.v7.min.js').read_bytes(),'text/javascript')
        if path=='/login':
            key=q.get('k','');return self.send(302,b'',headers=[('Location','/' if account(key) else '/?bad=1'),self.cookie(key)])
        if path=='/logout':return self.send(302,b'',headers=[('Location','/'),self.cookie('',0)])
        acct=account(self.key())
        if path=='/':
            if not acct:return self.send(200,landing(base_url(self),'That key was not recognised.' if q.get('bad') else ''),'text/html')
            return self.send(200,viewer_page(acct,base_url(self),self.key()),'text/html')
        if not acct:return self.send(401,{'error':'sign in'})
        st=store(acct['id']);vis,hidden=visible_sessions(acct);ids={s['id'] for s in vis}
        if path=='/api/me':return self.send(200,{'email':acct['email'],'plan':acct['plan'],'sessions':len(vis),'hidden_sessions':hidden,'locked':[] if acct['plan']=='pro' else LOCKED_FREE})
        if path=='/sessions':return self.send(200,{'current':'__cloud__','db':'AgentLens Cloud','sessions':vis,'hidden':hidden})  # no live in-process session: the viewer follows the newest stored one
        sid=q.get('session')
        if path in PRO_ROUTES:
            if acct['plan']!='pro':return self.send(402,{'paywall':True,'feature':PRO_ROUTES[path]})
            if path=='/diff':return self.send(200,rundiff.snapshot(st,q))
            fn={'/fleet':fleet.snapshot,'/facets':facets.snapshot,'/longterm':longterm.snapshot}[path]
            return self.send(200,fn(st))
        if path in ('/snapshot','/payload','/span_payloads','/events'):
            sid=sid or (vis[0]['id'] if vis else None)
            if sid not in ids:return self.send(402 if sid else 404,{'paywall':True,'feature':'history'} if sid else {'error':'no sessions yet'})
        if path=='/snapshot':
            snap=st.snapshot(sid) or {'session_id':sid,'events':[]}
            if acct['plan']!='pro':snap['events']=[e for e in snap['events'] if e['type'] not in PRO_EVENTS]
            snap['revision']=REV.get((acct['id'],sid),0);return self.send(200,snap)
        if path=='/payload':
            try:item=st.payload(sid,int(q.get('id','0')))
            except ValueError:item=None
            return self.send(200 if item else 404,item or {'error':'not found'})
        if path=='/span_payloads':return self.send(200,st.payloads_for_span(sid,q.get('span','')))
        if path=='/events':return self.stream(acct,sid)
        return self.send(404,{'error':'not found'})

    def stream(self,acct,sid):
        """Server-sent events: a session.reload event whenever new transcript lines rebuilt this session."""
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.send_header('X-Accel-Buffering','no');self.end_headers()
        seen=REV.get((acct['id'],sid),0);last_list=None;t0=time.time()
        try:
            while time.time()-t0<3600:
                rev=REV.get((acct['id'],sid),0)
                if rev!=seen:
                    seen=rev;self.wfile.write(('data: '+json.dumps({'type':'session.reload','session_id':sid,'revision':rev,'id':0})+'\n\n').encode())
                newest=[s['id'] for s in visible_sessions(acct)[0]][:1]
                if last_list is not None and newest!=last_list:self.wfile.write(('data: '+json.dumps({'type':'session.new','id':0,'session_id':newest[0] if newest else None})+'\n\n').encode())
                last_list=newest;self.wfile.write(b': heartbeat\n\n');self.wfile.flush();time.sleep(1.0)
        except (BrokenPipeError,ConnectionResetError):pass

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--port',type=int,default=int(os.environ.get('PORT','8080')));ap.add_argument('--host',default='0.0.0.0')
    a=ap.parse_args(argv);srv=ThreadingHTTPServer((a.host,a.port),H);srv.daemon_threads=True
    print(f'AgentLens Cloud on {a.host}:{a.port}  data={DATA}  dev={DEV}',flush=True);srv.serve_forever()

if __name__=='__main__':main()
