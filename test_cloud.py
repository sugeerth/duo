"""AgentLens Cloud: server, Stripe webhook, and the client watcher (no network beyond 127.0.0.1)."""
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class Cloud(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();os.environ['AGENTLENS_CLOUD_DATA']=cls.tmp.name
        cls.srv=load('cloud_server',ROOT/'cloud'/'server.py');cls.srv.DATA=Path(cls.tmp.name)
        from http.server import ThreadingHTTPServer
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),cls.srv.H);cls.http.daemon_threads=True
        threading.Thread(target=cls.http.serve_forever,daemon=True).start();cls.base=f'http://127.0.0.1:{cls.http.server_address[1]}'
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close()
    def req(self,path,body=None,key=None,headers=None,raw=None):
        h={'Content-Type':'application/json',**(headers or {})}
        if key:h['Authorization']='Bearer '+key
        data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r=urllib.request.Request(self.base+path,data=data,headers=h,method='POST' if data is not None else 'GET')
        try:
            with urllib.request.urlopen(r,timeout=20) as resp:return resp.status,json.loads(resp.read() or b'null')
        except urllib.error.HTTPError as e:return e.code,json.loads(e.read() or b'null')
    def signup(self,email='a@b.co'):
        code,j=self.req('/api/signup',{'email':email},headers={'X-AgentLens':'1'});self.assertEqual(code,200);return j['key']

    def test_signup_auth_and_csrf(self):
        self.assertEqual(self.req('/api/signup',{'email':'x@y.co'})[0],403)  # no custom header: cross-site forms cannot sign people up
        self.assertEqual(self.req('/api/signup',{'email':'nope'},headers={'X-AgentLens':'1'})[0],400)
        key=self.signup('c@d.co');self.assertTrue(key.startswith('al_'))
        self.assertEqual(self.req('/api/me')[0],401);code,me=self.req('/api/me',key=key);self.assertEqual((code,me['plan'],me['sessions']),(200,'free',0))
        with self.srv.adb() as db:self.assertNotIn(key,json.dumps(db.execute('SELECT * FROM accounts').fetchall()))  # stored hashed

    def test_ingest_rebuilds_live_and_free_plan_limits(self):
        key=self.signup('live@b.co');lines=(ROOT/'bench'/'data'/'imports'/'claude_code_session.jsonl').read_text().splitlines()
        c,j=self.req('/v1/transcripts',{'file_id':'f1','provider':'claude','lines':lines[:15]},key=key);self.assertEqual(c,200);sid=j['session']
        n1=sum(e['type']=='tool.started' for e in self.req('/snapshot?session='+sid,key=key)[1]['events'])
        c,j=self.req('/v1/transcripts',{'file_id':'f1','provider':'claude','lines':lines[15:]},key=key);self.assertEqual(j['session'],sid)
        snap=self.req('/snapshot?session='+sid,key=key)[1];n2=sum(e['type']=='tool.started' for e in snap['events'])
        self.assertGreater(n2,n1);self.assertGreaterEqual(snap['revision'],2)
        self.assertEqual(len(self.req('/sessions',key=key)[1]['sessions']),1)  # rebuilt, not duplicated
        self.assertFalse(any(e['type'] in self.srv.PRO_EVENTS for e in snap['events']))
        self.assertEqual(self.req('/fleet',key=key)[0],402)
        for i in range(4):self.req('/v1/transcripts',{'file_id':f'g{i}','provider':'claude','lines':[json.dumps({'type':'user','message':{'role':'user','content':f'task {i}'},'timestamp':'2026-11-19T10:00:0%dZ'%i})]},key=key);time.sleep(.02)
        s=self.req('/sessions',key=key)[1];self.assertEqual(len(s['sessions']),3);self.assertEqual(s['hidden'],2)
        self.assertEqual(self.req('/snapshot?session='+sid,key=key)[0],402)  # older than the free window of 3
        self.assertEqual(self.req('/v1/transcripts',{'file_id':'f1','lines':[]},key=key)[0],400)

    def test_stripe_signature_and_plan_changes(self):
        key=self.signup('pay@b.co');aid=self.srv.account(key)['id'];secret='whsec_test'
        ev=json.dumps({'type':'checkout.session.completed','data':{'object':{'client_reference_id':aid,'customer':'cus_1'}}}).encode()
        ts=int(time.time());sig=hmac.new(secret.encode(),f'{ts}.'.encode()+ev,hashlib.sha256).hexdigest()
        with patch.dict(os.environ,{'STRIPE_WEBHOOK_SECRET':secret}):
            self.assertEqual(self.req('/stripe/webhook',raw=ev,headers={'Stripe-Signature':f't={ts},v1=deadbeef'})[0],400)
            self.assertEqual(self.req('/stripe/webhook',raw=ev,headers={'Stripe-Signature':f't={ts-1000},v1='+hmac.new(secret.encode(),f'{ts-1000}.'.encode()+ev,hashlib.sha256).hexdigest()})[0],400)  # replayed
            c,j=self.req('/stripe/webhook',raw=ev,headers={'Stripe-Signature':f't={ts},v1={sig}'});self.assertEqual((c,j['result']),(200,'upgraded'))
            self.assertEqual(self.req('/api/me',key=key)[1]['plan'],'pro');self.assertEqual(self.req('/fleet',key=key)[0],200)
            down=json.dumps({'type':'customer.subscription.deleted','data':{'object':{'customer':'cus_1'}}}).encode();sig2=hmac.new(secret.encode(),f'{ts}.'.encode()+down,hashlib.sha256).hexdigest()
            self.assertEqual(self.req('/stripe/webhook',raw=down,headers={'Stripe-Signature':f't={ts},v1={sig2}'})[1]['result'],'downgraded')
        self.assertEqual(self.req('/api/me',key=key)[1]['plan'],'free')
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('STRIPE_SECRET_KEY',None)
            c,j=self.req('/api/checkout',{},key=key,headers={'X-AgentLens':'1'});self.assertEqual(c,503);self.assertIn('not configured',j['error'])
        self.assertEqual(self.req('/api/checkout',{},key=key)[0],403)

    def test_viewer_page_carries_plan_and_locks(self):
        key=self.signup('v@b.co');r=urllib.request.Request(self.base+'/',headers={'Cookie':'al_key='+key})
        with urllib.request.urlopen(r) as resp:page=resp.read().decode()
        self.assertIn('window.AGENTLENS_PAYWALL',page);self.assertIn('"plan": "free"',page);self.assertIn('install.sh | sh -s -- '+key,page)
        with urllib.request.urlopen(self.base+'/') as resp:self.assertIn('Start streaming',resp.read().decode())
        with urllib.request.urlopen(self.base+'/install.sh') as resp:self.assertIn(self.base,resp.read().decode())

    def test_account_guardrails_and_claude_code_hook(self):
        key=self.signup('rails@b.co');c,j=self.req('/api/guardrails',{'max_steps':2,'forbid_write':['vendor/**'],'junk':1},key=key);self.assertEqual((c,j['rules']),(200,['forbid_write','max_steps']))
        self.assertEqual(self.req('/api/guardrails',key=key)[1],{'max_steps':2,'forbid_write':['vendor/**']})
        lines=(ROOT/'bench'/'data'/'imports'/'claude_code_session.jsonl').read_text().splitlines()
        sid=self.req('/v1/transcripts',{'file_id':'gr1','provider':'claude','lines':lines},key=key)[1]['session']
        ev=[e for e in self.req('/snapshot?session='+sid,key=key)[1]['events'] if e['type']=='analysis.guardrails'];self.assertTrue(ev);self.assertTrue(any(e['count'] for e in ev))
        with tempfile.TemporaryDirectory() as home,patch.dict(os.environ,{'HOME':home}):
            snd=load('agentlens_send2',ROOT/'cloud'/'agentlens_send.py');pol={'forbid_write':['vendor/**'],'forbid_read':['.env'],'forbid_commands':['git push --force'],'max_steps':2}
            self.assertIn('vendor/x.py',snd.rail_check(pol,'Edit',{'file_path':'vendor/x.py'}));self.assertIsNone(snd.rail_check(pol,'Edit',{'file_path':'src/x.py'}))
            self.assertIn('vendor',snd.rail_check(pol,'Write',{'file_path':'/home/u/proj/vendor/lib/x.py'}));self.assertIsNone(snd.rail_check(pol,'Write',{'file_path':'/home/u/proj/src/vendors.py'}))
            self.assertIn('.env',snd.rail_check(pol,'Read',{'file_path':'/home/u/proj/.env'}))
            self.assertIn('.env',snd.rail_check(pol,'Bash',{'command':'cat .env'}));self.assertIn('not allowed',snd.rail_check(pol,'Bash',{'command':'git push --force origin main'}))
            self.assertIsNone(snd.rail_check(pol,'Bash',{'command':'pytest -q'}))
            tr=Path(home)/'t.jsonl';tr.write_text('\n'.join(json.dumps(x) for x in [{'type':'user','message':{'content':'old task'}},{'type':'assistant','message':{'content':[{'type':'tool_use'}]}},
                {'type':'user','message':{'content':'new task'},'timestamp':'2026-11-25T10:00:00Z'},{'type':'assistant','message':{'content':[{'type':'tool_use'}]}},{'type':'assistant','message':{'content':[{'type':'tool_use'}]}}]))
            self.assertIn('used its budget',snd.rail_check(pol,'Read',{'file_path':'a.py'},str(tr)));self.assertIsNone(snd.rail_check({**pol,'max_steps':3},'Read',{'file_path':'a.py'},str(tr)))
            (Path(home)/'.claude').mkdir();sf=Path(home)/'.claude'/'settings.json';sf.write_text('{}');snd.guard_hook(True);snd.guard_hook(True)
            self.assertEqual(len(json.loads(sf.read_text())['hooks']['PreToolUse']),1);snd.guard_hook(False);self.assertEqual(json.loads(sf.read_text()),{})

class Sender(unittest.TestCase):
    def test_redaction_and_hook_merge(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ,{'HOME':home}):
                snd=load('agentlens_send',ROOT/'cloud'/'agentlens_send.py')
                line=json.dumps({'out':'AKIAABCDEFGHIJKLMNOP and ghp_'+'a'*36+' sk-ant-api03-'+'b'*30+' password=hunter2hunter2 "token": "zzzzzzzzzz"'})
                red=snd.redact(line);self.assertEqual(json.loads(red)['out'].count('[REDACTED]'),5);self.assertNotIn('hunter2',red);json.loads(red)
                (Path(home)/'.claude').mkdir();sf=Path(home)/'.claude'/'settings.json';sf.write_text(json.dumps({'model':'x','hooks':{'PreToolUse':[{'matcher':'Bash','hooks':[{'type':'command','command':'echo'}]}]}}))
                snd.hook(True);snd.hook(True);s=json.loads(sf.read_text())
                self.assertEqual(len(s['hooks']['SessionStart']),1);self.assertEqual(s['model'],'x');self.assertEqual(len(s['hooks']['PreToolUse']),1)
                snd.hook(False);s=json.loads(sf.read_text());self.assertNotIn('SessionStart',s['hooks']);self.assertTrue((Path(home)/'.claude'/'settings.json.agentlens-backup').exists())

if __name__=='__main__':unittest.main()
