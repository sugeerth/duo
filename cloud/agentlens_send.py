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
    offsets=load(OFF,{});wait=INTERVAL
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
    print(f"Signed in as {me.get('email')} ({me.get('plan')} plan).");print(hook(True));start()
    print(f"Open {url.rstrip('/')}/login?k={key} to watch your sessions live.")

def main():
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);sub=ap.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('setup');s.add_argument('--url',required=True);s.add_argument('--key',required=True)
    for c in ('start','watch'):sub.add_parser(c).add_argument('--quiet',action='store_true')
    sub.add_parser('stop');sub.add_parser('status');sub.add_parser('uninstall')
    a=ap.parse_args()
    if a.cmd=='setup':setup(a.url,a.key)
    elif a.cmd=='start':start(a.quiet)
    elif a.cmd=='watch':watch(a.quiet)
    elif a.cmd=='stop':stop()
    elif a.cmd=='status':print(f'running (pid {running()})' if running() else 'not running')
    elif a.cmd=='uninstall':stop();print(hook(False));CFG.unlink(missing_ok=True)

if __name__=='__main__':main()
