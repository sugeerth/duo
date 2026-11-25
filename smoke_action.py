"""Live integration check: scratch-file write, independent review, synthesis."""
from pathlib import Path
import tempfile
from unittest.mock import patch
import agentlens as agentlens

def main():
 with tempfile.TemporaryDirectory(prefix='agentlens-action-check-') as tmp:
  root=Path(tmp)
  def approve(provider,action,details):
   # Never approve elevated access or external actions in this check.
   if action in ('Write','Edit'):
    path=details.get('file_path','')
    return bool(path) and Path(path).resolve().is_relative_to(root)
   return False
  with patch.object(Path,'cwd',return_value=root),patch.object(agentlens,'duo_approval',side_effect=approve):
   result=agentlens.unified_work('Implement this scratch task: create result.txt containing exactly AGENTLENS_ACTION_OK followed by a newline. Read it back to verify. Do not modify any other file.',auto_context=False,timeout=90)
  written=(root/'result.txt').exists() and (root/'result.txt').read_text()=='AGENTLENS_ACTION_OK\n'
  both=len(result['results'])==2 and all(r['ok'] for r in result['results'].values())
  print('Scratch file actually changed:',written)
  print('Both embedded agents completed:',both)
  return 0 if written and both else 1
if __name__=='__main__':raise SystemExit(main())
