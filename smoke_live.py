"""Verify embedded agents using a harmless temporary workspace."""
import concurrent.futures
from pathlib import Path
import tempfile
from agents import codex_run,claude_run

def main():
 with tempfile.TemporaryDirectory(prefix='agentlens-live-check-') as tmp:
  root=Path(tmp);(root/'probe.txt').write_text('AGENTLENS_FILE_OK\n')
  request='Read probe.txt in the current workspace using a file-reading tool. Reply only with its exact content. Do not modify anything.'
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   tasks={pool.submit(fn,request,root,lambda *args:False,lambda *args:None,readonly=True,timeout=60):provider for provider,fn in [('codex',codex_run),('claude',claude_run)]}
   good=True
   for future in concurrent.futures.as_completed(tasks):
    provider=tasks[future]
    try:result=future.result()
    except Exception as exc:result={'ok':False,'error':str(exc)}
    passed=result.get('ok') and 'AGENTLENS_FILE_OK' in result.get('answer','')
    print(provider+': '+('PASS ('+str(result.get('seconds'))+'s)' if passed else 'FAIL '+str(result.get('error',result.get('answer','')))[:900]),flush=True)
    good=good and passed
  return 0 if good else 1
if __name__=='__main__':raise SystemExit(main())
