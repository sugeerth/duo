"""Durable trace store: one SQLite file for every AgentLens session.

Schema follows the OpenTelemetry GenAI shape (session -> span -> event) plus two
trace-specific tables: payloads (reasoning, tool input/output, answers, prompts,
fetched on demand, never streamed) and judge_scores (one row per judge x model x
dimension, so model comparisons are a single SQL query).

Writes go through a background thread so tracing never blocks an agent.
Location: ~/.agentlens/traces.db, override with AGENTLENS_TRACE_DB=/path, disable with AGENTLENS_TRACE_DB=off.
"""
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading

SCHEMA='''
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started REAL, cwd TEXT, last_ts REAL, turns INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(session_id TEXT, id INTEGER, ts REAL, type TEXT, provider TEXT, span_id TEXT, data TEXT, PRIMARY KEY(session_id,id));
CREATE TABLE IF NOT EXISTS spans(span_id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, kind TEXT, provider TEXT, role TEXT, mode TEXT,
  turn_id TEXT, start REAL, end REAL, status TEXT, duration_ms INTEGER, tools INTEGER, tool_errors INTEGER);
CREATE TABLE IF NOT EXISTS payloads(session_id TEXT, event_id INTEGER, span_id TEXT, provider TEXT, kind TEXT, text TEXT, PRIMARY KEY(session_id,event_id));
CREATE TABLE IF NOT EXISTS judge_scores(session_id TEXT, turn_id TEXT, judge TEXT, provider TEXT, dimension TEXT, score REAL, ts REAL);
CREATE INDEX IF NOT EXISTS events_type ON events(session_id,type);
CREATE INDEX IF NOT EXISTS spans_session ON spans(session_id,kind);
CREATE INDEX IF NOT EXISTS judge_cmp ON judge_scores(provider,dimension);
'''
PAYLOAD_CHARS=20000  # per payload; reasoning and tool output beyond this are truncated

def default_path():
    value=os.environ.get('AGENTLENS_TRACE_DB','')
    if value.lower() in ('off','0','none','false'):return None
    return Path(value).expanduser() if value else Path.home()/'.agentlens'/'traces.db'

class TraceStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.queue=queue.Queue();self.lock=threading.Lock()
        with self.connect() as db:db.executescript(SCHEMA)
        threading.Thread(target=self._writer,daemon=True,name='agentlens-tracestore').start()
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10,check_same_thread=False);db.execute('PRAGMA journal_mode=WAL');return db
    # --- writes (async) ---
    def session(self,session_id,started,cwd):self.queue.put(('session',(session_id,started,cwd)))
    def event(self,event,payload=None,payload_kind=None):self.queue.put(('event',(event,payload,payload_kind)))
    def flush(self,timeout=5):
        done=threading.Event();self.queue.put(('flush',done));done.wait(timeout)
    def _writer(self):
        db=self.connect()
        while True:
            batch=[self.queue.get()]
            while True:
                try:batch.append(self.queue.get_nowait())
                except queue.Empty:break
            flushes=[]
            with self.lock:
                try:
                    for kind,item in batch:
                        if kind=='flush':flushes.append(item)
                        elif kind=='session':db.execute('INSERT OR IGNORE INTO sessions(id,started,cwd,last_ts) VALUES(?,?,?,?)',(*item,item[1]))
                        else:self._write_event(db,*item)
                    db.commit()
                except sqlite3.Error:db.rollback()
            for done in flushes:done.set()
    def _write_event(self,db,e,payload,payload_kind):
        sid=e['session_id'];kind=e['type']
        data={k:v for k,v in e.items() if k not in ('session_id','id','timestamp','type','provider','span_id')}
        db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?)',(sid,e['id'],e['timestamp'],kind,e.get('provider'),e.get('span_id'),json.dumps(data)))
        db.execute('UPDATE sessions SET last_ts=? WHERE id=?',(e['timestamp'],sid))
        if kind=='span.start':
            db.execute('INSERT OR REPLACE INTO spans(span_id,session_id,parent_id,kind,provider,role,mode,turn_id,start,status) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (e['span_id'],sid,e.get('parent_id'),e.get('span_kind'),e.get('provider'),e.get('role'),e.get('mode'),e.get('turn_id'),e['timestamp'],'running'))
            if e.get('span_kind')=='turn':db.execute('UPDATE sessions SET turns=turns+1 WHERE id=?',(sid,))
        elif kind=='span.end':
            db.execute('UPDATE spans SET end=?,status=?,duration_ms=?,tools=?,tool_errors=? WHERE span_id=?',
                (e['timestamp'],e.get('status'),e.get('duration_ms'),e.get('tools'),e.get('tool_errors'),e['span_id']))
        elif kind=='judge.score':
            db.execute('INSERT INTO judge_scores VALUES(?,?,?,?,?,?,?)',(sid,e.get('turn_id'),e.get('judge'),e.get('provider'),e.get('name'),e.get('score'),e['timestamp']))
        if payload:
            db.execute('INSERT OR REPLACE INTO payloads VALUES(?,?,?,?,?,?)',(sid,e['id'],e.get('span_id'),e.get('provider'),payload_kind,payload[:PAYLOAD_CHARS]))
    # --- reads ---
    def sessions(self,limit=50):
        with self.lock,self.connect() as db:
            rows=db.execute('SELECT id,started,cwd,last_ts,turns FROM sessions ORDER BY started DESC LIMIT ?',(limit,)).fetchall()
        return [dict(zip(('id','started','cwd','last_ts','turns'),r)) for r in rows]
    def snapshot(self,session_id):
        with self.lock,self.connect() as db:
            row=db.execute('SELECT started FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not row:return None
            rows=db.execute('SELECT id,ts,type,provider,span_id,data FROM events WHERE session_id=? ORDER BY id',(session_id,)).fetchall()
        events=[]
        for id_,ts,kind,provider,span_id,data in rows:
            e={'id':id_,'timestamp':ts,'session_id':session_id,'type':kind,'provider':provider,**json.loads(data)}
            if span_id:e['span_id']=span_id
            events.append(e)
        return {'session_id':session_id,'started':row[0],'events':events,'stored':True}
    def add_event(self,session_id,event):
        """Append an event to an existing (possibly past) session, e.g. a calibration label. Synchronous."""
        self.flush(3)
        import time as _t
        with self.lock,self.connect() as db:
            next_id=(db.execute('SELECT COALESCE(MAX(id),0)+1 FROM events WHERE session_id=?',(session_id,)).fetchone()[0])
            data={k:v for k,v in event.items() if k not in ('type','provider','span_id')}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',(session_id,next_id,_t.time(),event['type'],event.get('provider','duo'),event.get('span_id'),json.dumps(data)))
            db.commit()
        return next_id
    def events_of_type(self,kind,limit=5000):
        """Events of one type across every stored session (e.g. judge.action for cross-session lessons)."""
        with self.lock,self.connect() as db:
            rows=db.execute('SELECT session_id,id,ts,provider,span_id,data FROM events WHERE type=? ORDER BY ts DESC LIMIT ?',(kind,limit)).fetchall()
        return [{'session_id':sid,'id':i,'timestamp':ts,'type':kind,'provider':p,'span_id':sp,**json.loads(d)} for sid,i,ts,p,sp,d in rows]
    def events_since(self,session_id,after,limit=2000):
        """Live tail: events of one session newer than `after` (used by /events?session=…)."""
        try:
            with self.lock,self.connect() as db:
                rows=db.execute('SELECT id,ts,type,provider,span_id,data FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT ?',(session_id,after,limit)).fetchall()
        except sqlite3.Error:return []  # DB removed or closing mid-tail; the SSE loop just sees no news
        out=[]
        for id_,ts,kind,provider,span_id,data in rows:
            e={'id':id_,'timestamp':ts,'session_id':session_id,'type':kind,'provider':provider,**json.loads(data)}
            if span_id:e['span_id']=span_id
            out.append(e)
        return out
    def payload(self,session_id,event_id):
        with self.lock,self.connect() as db:
            row=db.execute('SELECT kind,provider,span_id,text FROM payloads WHERE session_id=? AND event_id=?',(session_id,event_id)).fetchone()
        return dict(zip(('kind','provider','span_id','text'),row)) if row else None
    def payloads_for_span(self,session_id,span_id):
        with self.lock,self.connect() as db:
            rows=db.execute('SELECT event_id,kind,provider,text FROM payloads WHERE session_id=? AND span_id=? ORDER BY event_id',(session_id,span_id)).fetchall()
        return [dict(zip(('event_id','kind','provider','text'),r)) for r in rows]

def open_default():
    path=default_path()
    if not path:return None
    try:return TraceStore(path)
    except (OSError,sqlite3.Error):return None
