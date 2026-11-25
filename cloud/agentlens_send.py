#!/usr/bin/env python3
"""agentlens-send: stream your Claude Code and Codex transcripts to AgentLens Cloud, live. Python 3.8+, no packages.

    python3 ~/.agentlens/agentlens-send.py setup --url URL --key KEY   # what the install line runs
    python3 ~/.agentlens/agentlens-send.py start | stop | status | watch

It reads only what the agents already write on this machine:
  Claude Code  ~/.claude/projects/**/*.jsonl
  Codex        ~/.codex/sessions/**/*.jsonl
New complete lines of files changed since setup (minus BACKFILL_HOURS) are sent every ~1.5 s, after secrets are
removed here: API keys and tokens (AWS, GitHub, OpenAI, Anthropic, Slack, Stripe, Google), private key blocks,
bearer tokens, and password= / secret= / token= values. `setup` also adds a Claude Code SessionStart hook that starts
the watcher when a session starts (your settings file is backed up first). `stop` stops it; `uninstall` removes it.
"""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME=Path.home();DIR=HOME/'.agentlens';CFG=DIR/'cloud.json';OFF=DIR/'cloud-offsets.json';PID=DIR/'cloud-send.pid';LOG=DIR/'cloud-send.log'
ROOTS=[('claude',HOME/'.claude'/'projects'),('codex',HOME/'.codex'/'sessions')]
BACKFILL_HOURS=2;INTERVAL=1.5;MAX_BATCH=800
HOOK_CMD='python3 ~/.agentlens/agentlens-send.py start --quiet'
GUARD_CMD='python3 ~/.agentlens/agentlens-send.py hook-check'
RAILS=DIR/'guardrails.json'

SECRETS=[re.compile(p) for p in (
    r'AKIA[0-9A-Z]{16}',r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}',r'github_pat_[A-Za-z0-9_]{40,}',r'sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}',
    r'xox[baprs]-[A-Za-z0-9-]{10,}',r'(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}',r'AIza[0-9A-Za-z_\-]{35}',r'al_[A-Za-z0-9_\-]{30,}',
    r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----',r'(?i)bearer\s+[A-Za-z0-9._\-]{20,}')]
ASSIGN=re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key)(\\?["\']?\s*[:=]\s*\\?["\']?)([^\s"\'\\,}]{6,})')

def redact(line):
    for p in SECRETS:line=p.sub('[REDACTED]',line)
    return ASSIGN.sub(lambda m:m.group(1)+m.group(2)+'[REDACTED]',line)

def load(p,default):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return default
def save(p,obj):
    tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(obj));os.replace(tmp,p)

def post(cfg,body):
    req=urllib.request.Request(cfg['url'].rstrip('/')+'/v1/transcripts',data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['key'],'User-Agent':'agentlens-send/0.1'})
    with urllib.request.urlopen(req,timeout=30) as r:return json.loads(r.read())

def scan(cfg,offsets):
    since=cfg.get('since',0)-BACKFILL_HOURS*3600;sent=0
    for provider,root in ROOTS:
        if not root.is_dir():continue
        for f in root.rglob('*.jsonl'):
            try:st=f.stat()
            except OSError:continue
            key=str(f);off=offsets.get(key,0)
            if st.st_mtime<since or st.st_size<=off:continue
            with f.open('rb') as fh:
                fh.seek(off);chunk=fh.read(8_000_000)
            end=chunk.rfind(b'\n')
            if end<0:continue
            lines=[redact(l) for l in chunk[:end].decode('utf-8','replace').split('\n') if l.strip()]
            for i in range(0,len(lines),MAX_BATCH):
                post(cfg,{'file_id':hashlib.sha1(key.encode()).hexdigest()[:16],'provider':provider,'name':f.stem,'lines':lines[i:i+MAX_BATCH]})
            offsets[key]=off+end+1;sent+=len(lines);save(OFF,offsets)
    return sent

def watch(quiet=False):
    cfg=load(CFG,None)
    if not cfg:sys.exit('Not set up: run the install line from your AgentLens page.')
    offsets=load(OFF,{});wait=INTERVAL;push_rails(cfg)
    while True:
        try:
            n=scan(cfg,offsets);wait=INTERVAL
            if n and not quiet:print(f'sent {n} lines',flush=True)
        except Exception as exc:
            wait=min(60,wait*2)
            if not quiet:print(f'retrying in {wait:.0f}s: {exc}',flush=True)
        time.sleep(wait)

def running():
    try:pid=int(PID.read_text());os.kill(pid,0);return pid
    except (OSError,ValueError):return None

def start(quiet=False):
    if running():
        if not quiet:print('agentlens-send is already running.')
        return
    DIR.mkdir(exist_ok=True)
    with LOG.open('a') as log:
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'watch','--quiet'],stdout=log,stderr=log,stdin=subprocess.DEVNULL,start_new_session=True)
    PID.write_text(str(p.pid))
    if not quiet:print('agentlens-send is running: new Claude Code and Codex steps stream live.')

def stop():
    pid=running()
    if pid:os.kill(pid,signal.SIGTERM);print('Stopped.')
    else:print('Not running.')
    PID.unlink(missing_ok=True)

# ---- guardrails inside Claude Code: a PreToolUse hook. Exit code 2 refuses the call and shows the reason to Claude.
def _globhit(path,pats):
    import fnmatch
    path=str(path or '');path=path[2:] if path.startswith('./') else path
    for g in pats or []:
        g2=g[2:] if g.startswith('./') else g
        if fnmatch.fnmatch(path,g2) or fnmatch.fnmatch(path,'*/'+g2) or fnmatch.fnmatch(path.split('/')[-1],g2) or (g2.endswith('/**') and ('/'+g2[:-2]) in ('/'+path)):return g  # Claude Code passes absolute paths
    return None

def rail_check(pol,tool,inp,transcript=None,now=None):
    """-> reason the call must be refused, or None. Same rules as guardrails.py (cost is checked after the run, on the server)."""
    inp=inp if isinstance(inp,dict) else {};path=inp.get('file_path') or inp.get('path') or inp.get('notebook_path') or '';cmd=str(inp.get('command') or '')
    if tool in ('Edit','Write','MultiEdit','NotebookEdit') and (g:=_globhit(path,pol.get('forbid_write'))):return f'{path} may not be changed ({g})'
    if tool in ('Read','Grep','Glob') and (g:=_globhit(path,pol.get('forbid_read'))):return f'{path} may not be read ({g})'
    if cmd:
        for word in re.findall(r'[\w./~-]+',cmd):
            if (g:=_globhit(word,pol.get('forbid_read'))):return f'{word} may not be read ({g})'
            if re.search(r'(>|\btee\b|\bsed -i\b|\brm\b|\bmv\b)',cmd) and (g:=_globhit(word,pol.get('forbid_write'))):return f'{word} may not be changed ({g})'
        for rx in pol.get('forbid_commands') or []:
            try:
                if re.search(rx,cmd,re.I):return f'this command is not allowed ({rx})'
            except re.error:pass
    if transcript and any(k in pol for k in ('max_steps','max_failed_calls','max_minutes')):
        steps=fails=0;start=None
        try:lines=Path(transcript).read_text(errors='replace').splitlines()
        except OSError:lines=[]
        for line in reversed(lines):
            try:d=json.loads(line)
            except ValueError:continue
            c=(d.get('message') or {}).get('content')
            if d.get('type')=='user' and (isinstance(c,str) or (isinstance(c,list) and not any(isinstance(x,dict) and x.get('type')=='tool_result' for x in c))):start=d.get('timestamp');break
            for x in c if isinstance(c,list) else []:
                if isinstance(x,dict) and x.get('type')=='tool_use':steps+=1
                if isinstance(x,dict) and x.get('type')=='tool_result' and x.get('is_error'):fails+=1
        mins=None
        if start:
            try:
                from datetime import datetime
                mins=((now or time.time())-datetime.fromisoformat(start.replace('Z','+00:00')).timestamp())/60
            except ValueError:pass
        for rule,val,label in (('max_steps',steps,'tool calls'),('max_failed_calls',fails,'failed calls'),('max_minutes',mins,'minutes')):
            if rule in pol and val is not None and val>=pol[rule]:return f'this task has used its budget: {int(val)} {label}, limit {pol[rule]}. Stop here and report what is done and what is left'
    return None

def hook_check():
    pol=load(RAILS,{})
    if not pol:return 0
    try:d=json.loads(sys.stdin.read() or '{}')
    except ValueError:return 0
    why=rail_check(pol,d.get('tool_name'),d.get('tool_input'),d.get('transcript_path'))
    if why:print('Blocked by an AgentLens guardrail: '+why+'.',file=sys.stderr);return 2
    return 0

def guard_hook(add=True):
    """Add (or remove) the PreToolUse hook that enforces ~/.agentlens/guardrails.json inside Claude Code."""
    sf=HOME/'.claude'/'settings.json'
    if not sf.parent.is_dir():return 'Claude Code not found; guardrails are reported but not enforced.'
    s=load(sf,{}) if sf.exists() else {}
    hooks=s.setdefault('hooks',{}).setdefault('PreToolUse',[])
    hooks[:]=[m for m in hooks if not any(h.get('command')==GUARD_CMD for h in m.get('hooks',[]))]
    if add:hooks.append({'hooks':[{'type':'command','command':GUARD_CMD,'timeout':10}]})
    if not hooks:s['hooks'].pop('PreToolUse')
    if not s['hooks']:s.pop('hooks')
    sf.write_text(json.dumps(s,indent=2)+'\n')
    return 'Guardrails are enforced inside Claude Code.' if add else 'Guardrail enforcement removed from Claude Code.'

def push_rails(cfg):
    """Send the local guardrails to the dashboard, so runs are reported against them."""
    pol=load(RAILS,None)
    if pol is None:return
    try:
        req=urllib.request.Request(cfg['url'].rstrip('/')+'/api/guardrails',data=json.dumps(pol).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['key']})
        urllib.request.urlopen(req,timeout=15).read()
    except Exception:pass

def hook(add=True):
    """Add (or remove) a Claude Code SessionStart hook that starts the watcher. Backs up settings.json first."""
    sf=HOME/'.claude'/'settings.json'
    if not sf.parent.is_dir():return 'Claude Code not found; skipped the auto-start hook.'
    s=load(sf,{}) if sf.exists() else {}
    if sf.exists():sf.with_name('settings.json.agentlens-backup').write_text(sf.read_text())
    hooks=s.setdefault('hooks',{}).setdefault('SessionStart',[])
    hooks[:]=[m for m in hooks if not any(h.get('command')==HOOK_CMD for h in m.get('hooks',[]))]
    if add:hooks.append({'hooks':[{'type':'command','command':HOOK_CMD}]})
    if not hooks:s['hooks'].pop('SessionStart')
    if not s['hooks']:s.pop('hooks')
    sf.write_text(json.dumps(s,indent=2)+'\n')
    return 'Claude Code will start the watcher with each session.' if add else 'Removed the Claude Code hook.'

def setup(url,key):
    DIR.mkdir(exist_ok=True);save(CFG,{'url':url.rstrip('/'),'key':key,'since':time.time()});CFG.chmod(0o600)
    if not OFF.exists():save(OFF,{})
    try:
        r=urllib.request.Request(url.rstrip('/')+'/api/me',headers={'Authorization':'Bearer '+key})
        with urllib.request.urlopen(r,timeout=15) as resp:me=json.loads(resp.read())
    except Exception as exc:sys.exit(f'Could not reach AgentLens at {url} with that key: {exc}')
    print(f"Signed in as {me.get('email')} ({me.get('plan')} plan).");print(hook(True))
    if RAILS.exists():print(guard_hook(True));push_rails(load(CFG,{}))
    else:print('No guardrails yet: put limits in ~/.agentlens/guardrails.json, then run: python3 ~/.agentlens/agentlens-send.py guard on')
    start()
    print(f"Open {url.rstrip('/')}/login?k={key} to watch your sessions live.")

def main():
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);sub=ap.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('setup');s.add_argument('--url',required=True);s.add_argument('--key',required=True)
    for c in ('start','watch'):sub.add_parser(c).add_argument('--quiet',action='store_true')
    sub.add_parser('stop');sub.add_parser('status');sub.add_parser('uninstall');sub.add_parser('hook-check');sub.add_parser('guard').add_argument('state',choices=('on','off'))
    a=ap.parse_args()
    if a.cmd=='setup':setup(a.url,a.key)
    elif a.cmd=='start':start(a.quiet)
    elif a.cmd=='watch':watch(a.quiet)
    elif a.cmd=='stop':stop()
    elif a.cmd=='status':print(f'running (pid {running()})' if running() else 'not running')
    elif a.cmd=='hook-check':sys.exit(hook_check())
    elif a.cmd=='guard':
        print(guard_hook(a.state=='on'))
        if a.state=='on':
            if not RAILS.exists():print(f'Now put your limits in {RAILS}.')
            elif load(CFG,None):push_rails(load(CFG,{}))
    elif a.cmd=='uninstall':stop();print(hook(False));print(guard_hook(False));CFG.unlink(missing_ok=True)

if __name__=='__main__':main()
