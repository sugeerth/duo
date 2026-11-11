import json
import contextlib
import importlib.util
import io
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('duo',Path(__file__).with_name('duo.py'));duo=importlib.util.module_from_spec(spec);spec.loader.exec_module(duo)
class Tests(unittest.TestCase):
 def run_case(self,fake,mode='both'):
  with patch.object(duo,'call',side_effect=fake) as calls,patch.object(Path,'write_text',side_effect=AssertionError('Response write')),contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()):
   result=duo.run_turn('question',mode=mode)
  return result,calls.call_count,out.getvalue()
 def test_parallel_and_synthesis(self):
  barrier=threading.Barrier(2)
  def fake(provider,prompt,directory,*args):
   if directory.name!='judge':barrier.wait(timeout=2)
   return {'ok':True,'answer':'combined' if directory.name=='judge' else provider}
  result,n,out=self.run_case(fake);self.assertTrue(result['ok']);self.assertEqual(n,3);self.assertIn('combined',out)
 def test_separate_two_calls(self):
  result,n,out=self.run_case(lambda *a:{'ok':True,'answer':'answer'},'separate');self.assertEqual(n,2);self.assertNotIn('synthesized',out)
 def test_single_one_call(self):
  result,n,out=self.run_case(lambda *a:{'ok':True,'answer':'answer'},'claude');self.assertEqual(n,1)
 def test_one_failure_preserves_other(self):
  result,n,out=self.run_case(lambda provider,*a:{'ok':True,'answer':'good'} if provider=='codex' else {'ok':False,'error':'failed'});self.assertTrue(result['ok']);self.assertEqual(n,2);self.assertIn('good',out)
 def test_codex_parses_json_without_output_file(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(duo.shutil,'which',return_value='/mock/codex'),patch.object(duo.subprocess,'Popen') as popen:
   proc=popen.return_value;proc.communicate.return_value=('{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}','');proc.returncode=0
   result=duo.call('codex','q',Path(tmp),1);self.assertEqual(result['answer'],'answer');self.assertNotIn('-o',popen.call_args.args[0]);self.assertEqual(list(Path(tmp).iterdir()),[])
 def test_auto_context_attached_and_shared(self):
  captured=[]
  def fake(provider,prompt,*args):captured.append(prompt);return {'ok':True,'answer':'answer'}
  with tempfile.TemporaryDirectory() as tmp:
   board=Path(tmp)/'board.html';board.write_text('<h1>Plan</h1><p>Ship the recommender first.</p><script>secret()</script>')
   with patch.object(duo,'AUTO_CONTEXT_FILE',board):
    result,n,out=self.run_case(fake)  # unrelated question stays uncluttered
    self.assertNotIn(str(board),captured[0])
    with patch.object(duo,'call',side_effect=fake),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
     duo.run_turn('Highest ROI advice from my command center')
    self.assertTrue(all(str(board.resolve()) in prompt for prompt in captured[-3:]))
    self.assertTrue(all('secret()' not in prompt for prompt in captured[-3:]))
  self.assertTrue(all('recommender' in prompt.lower() for prompt in captured[-3:]))
 def test_auto_context_off_or_already_attached(self):
  with tempfile.TemporaryDirectory() as tmp:
   board=Path(tmp)/'board.html';board.write_text('<p>plan</p>')
   with patch.object(duo,'AUTO_CONTEXT_FILE',board):
    self.assertEqual(duo.resolve_context('command center',[],[],False),[])  # --no-auto-context
    self.assertEqual(len(duo.resolve_context('command center',[duo.context_file(board)],[])),1)  # attached by hand: not twice
  with patch.object(duo,'AUTO_CONTEXT_FILE',None):
   self.assertEqual(duo.resolve_context('command center',[],[]),[])  # DUO_AUTO_CONTEXT unset
 def test_demo_trace_writes_complete_judged_session(self):
  spec=importlib.util.spec_from_file_location('demo_trace',Path(__file__).with_name('tools')/'demo_trace.py');demo=importlib.util.module_from_spec(spec);spec.loader.exec_module(demo)
  with tempfile.TemporaryDirectory() as tmp:
   sid,n=demo.build(Path(tmp)/'demo.db');con=sqlite3.connect(Path(tmp)/'demo.db')
   self.assertEqual(con.execute('select count(*) from events').fetchone()[0],n)
   self.assertEqual(con.execute("select count(*) from spans where kind='turn'").fetchone()[0],len(demo.TURNS))
   self.assertEqual(con.execute('select count(*) from spans where end is null').fetchone()[0],0)
   self.assertEqual({r[0] for r in con.execute('select distinct provider from judge_scores')},{'codex','claude'})
   self.assertEqual(demo.build(Path(tmp)/'again.db'),(sid,n))  # seeded: reruns match the README screenshots
 def test_unified_readonly_runs_both_concurrently(self):
  barrier=threading.Barrier(2)
  def fake(provider,prompt,readonly=False,*args,**kwargs):
   self.assertTrue(readonly);barrier.wait(timeout=2);return {'ok':True,'answer':provider,'seconds':.1}
  with patch.object(duo,'agent_backend',side_effect=fake),patch.object(duo,'call',return_value={'ok':True,'answer':'merged'}),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   result=duo.unified_work('Read the project',auto_context=False)
  self.assertTrue(result['ok']);self.assertEqual(result['answer'],'merged')
 def test_write_review_repair_then_synthesis(self):
  calls=[]
  def fake(provider,prompt,readonly=False,**kwargs):
   calls.append((provider,readonly))
   if readonly:return {'ok':True,'answer':'{"verdict":"changes_required","summary":"bug","blocking_findings":[{"problem":"bug","evidence":"file","fix":"correct it"}]}','seconds':.1}
   return {'ok':True,'answer':'implemented and checked','seconds':.2}
  with patch.object(duo,'agent_backend',side_effect=fake),patch.object(duo,'call',return_value={'ok':True,'answer':'fixed'}),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   result=duo.unified_work('Redesign the HTML app',auto_context=False)
  self.assertEqual(calls,[('codex',False),('claude',True),('codex',False)]);self.assertTrue(result['repair']['ok'])
 def test_pass_does_not_repair(self):
  def fake(provider,prompt,readonly=False,**kwargs):return {'ok':True,'answer':'{"verdict":"pass","summary":"ok","blocking_findings":[]}' if readonly else 'done','seconds':.1}
  with patch.object(duo,'agent_backend',side_effect=fake) as backend,patch.object(duo,'call',return_value={'ok':True,'answer':'done'}),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
   result=duo.unified_work('Improve this app',auto_context=False)
  self.assertEqual(backend.call_count,2);self.assertIsNone(result['repair'])
 def test_intent_routes_action_and_advice(self):
  self.assertEqual(duo.task_intent('Redesign the command center'),'write')
  self.assertEqual(duo.task_intent('Can you make this app better?'),'write')
  self.assertEqual(duo.task_intent('Implement it'),'write')
  self.assertEqual(duo.task_intent('How should I improve my career?'),'respond')
  self.assertEqual(duo.task_intent('Just explain this code; do not edit'),'respond')
  self.assertEqual(duo.task_intent('Read the command center'),'read')
 def test_write_scope_restriction_does_not_disable_requested_write(self):
  self.assertEqual(duo.task_intent('Create result.txt. Do not modify any other file.'),'write')
  self.assertEqual(duo.task_intent('Edit app.py; do not change other files.'),'write')
  self.assertEqual(duo.task_intent('Inspect the repo; do not modify files.'),'read')
 def test_authorized_followup_inherits_user_task(self):
  history=[{'role':'user','text':'Review the billing folder in my home directory'},
           {'role':'assistant','text':'Do not execute commands or modify files.'}]
  for followup in ['yes', 'pls allow it', 'You have permission', 'go ahead', 'do it']:
   self.assertEqual(duo.task_intent(followup,history),'read')
   self.assertTrue(duo.needs_tools(followup,history))
  self.assertEqual(duo.task_intent('do it',[{'role':'user','text':'Improve the app'}]),'write')
  self.assertEqual(duo.task_intent('thanks',history),'respond')
 def test_folder_inspection_and_no_write_restriction(self):
  self.assertEqual(duo.task_intent('Analyze the billing folder'),'read')
  self.assertEqual(duo.task_intent('Review this directory; do not modify files'),'read')
 def test_authorized_inspection_has_tools_without_response_prohibition(self):
  def fake(provider,prompt,readonly=False,**kwargs):
   self.assertTrue(readonly)
   self.assertIn('Read-only file inspection is authorized',prompt)
   self.assertNotIn('Do not execute commands',prompt)
   return {'ok':True,'answer':'Inspected files','seconds':.1}
  with patch.object(duo,'agent_backend',side_effect=fake),contextlib.redirect_stdout(io.StringIO()):
   result=duo.unified_work('yes',only='codex',history=[{'role':'user','text':'Review the billing folder'}],auto_context=False)
  self.assertTrue(result['ok'])
 def test_trace_metadata_does_not_include_response(self):
  from traces import TraceHub
  hub=TraceHub();event=hub.emit('tool','codex',tool='Read',answer='private',prompt='private')
  self.assertNotIn('answer',event);self.assertNotIn('prompt',event);self.assertEqual(event['tool'],'Read')
 def test_approval_denial(self):
  import agents
  bridge=agents.CodexBridge('/tmp',lambda *a:False,lambda *a:None)
  with patch.object(bridge,'send') as send:
   bridge.handle_request({'id':2,'method':'item/commandExecution/requestApproval','params':{'command':'example'}})
  self.assertEqual(send.call_args.args[0]['result']['decision'],'decline')
 def test_trace_spans_form_tree_across_threads(self):
  from traces import TraceHub
  hub=TraceHub()
  def child():
   with hub.span('agent','codex',role='reader'):
    hub.emit('tool.started','codex',tool='Read');hub.emit('tool.completed','codex',tool='Read',passed=False)
  with hub.span('turn',mode='work:read') as turn:
   worker=threading.Thread(target=hub.bind(turn,child));worker.start();worker.join()
  agent=[e for e in hub.events if e['type']=='span.start' and e['span_kind']=='agent'][0]
  self.assertEqual(agent['parent_id'],turn.id)
  end=[e for e in hub.events if e['type']=='span.end' and e['span_id']==agent['span_id']][0]
  self.assertEqual((end['tools'],end['tool_errors']),(1,1))
  self.assertEqual([e for e in hub.events if e['type']=='tool.started'][0]['span_id'],agent['span_id'])
 def test_judge_blind_panel_maps_labels_and_emits(self):
  import judge
  seen=[]
  def fake(provider,prompt,directory,timeout,model=None,quiet=False):
   self.assertTrue(quiet);seen.append(prompt)
   self.assertNotIn('Codex',prompt.split('\n\n',1)[1])
   body=json.loads(prompt.split('\n\n',1)[1]);good=[k for k,v in body['candidates'].items() if v['answer']=='good']
   bad=[k for k in body['candidates'] if k not in good]
   return {'ok':True,'answer':json.dumps({'candidates':{good[0]:{'scores':{d:5 for d in judge.DIMENSIONS},'failure_tags':[]},bad[0]:{'scores':{'correctness':'2','completeness':9},'failure_tags':['hallucination','made_up_tag']}},'winner':good[0],'confidence':2,'rationale':'x'*999})}
  start=len(duo.HUB.events)
  results=judge.judge_turn('panel','turn123','q',{'codex':'good','claude':'bad'},fake,background=False)
  self.assertEqual(len(seen),4);self.assertEqual([r['winner'] for r in results],['codex','codex']);self.assertTrue(all(r['consistent'] for r in results))
  claude=results[0]['per']['claude'];self.assertEqual(claude['scores']['completeness'],5);self.assertEqual(claude['scores']['correctness'],2);self.assertEqual(claude['tags'],['hallucination'])
  self.assertEqual(results[0]['confidence'],1.0);self.assertLessEqual(len(results[0]['rationale']),judge.RATIONALE_CHARS)
  ev=list(duo.HUB.events)[start:]
  self.assertEqual({e['verdict'] for e in ev if e['type']=='judge.result' and e['provider']=='codex'},{'win'})
  self.assertTrue([e for e in ev if e['type']=='judge.agreement'][0]['passed'])
  self.assertFalse([e for e in ev if e['type']=='evaluation'])  # graders never count as contestant evals
 def test_judge_unparseable_is_reported_not_raised(self):
  import judge
  start=len(duo.HUB.events)
  self.assertEqual(judge.judge_turn('claude','t','q',{'codex':'a'},lambda *a,**k:{'ok':True,'answer':'not json'},background=False),[])
  self.assertEqual([e['reason_code'] for e in list(duo.HUB.events)[start:] if e['type']=='judge.error'],['unparseable'])
 def test_quiet_call_emits_nothing(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(duo.shutil,'which',return_value='/mock/claude'),patch.object(duo.subprocess,'Popen') as popen:
   proc=popen.return_value;proc.communicate.return_value=('answer','');proc.returncode=0
   start=len(duo.HUB.events);duo.call('claude','q',Path(tmp),1,quiet=True);self.assertEqual(len(duo.HUB.events),start)
 def test_trace_store_roundtrip_and_payloads(self):
  import traces,tracestore
  with tempfile.TemporaryDirectory() as tmp:
   hub=traces.TraceHub();store=tracestore.TraceStore(Path(tmp)/'t.db');hub.attach_store(store)
   with hub.span('turn',mode='respond:both') as turn:
    with hub.span('agent','claude',role='responder'):
     r=hub.emit('reasoning','claude',payload='step one')
     hub.emit('tool.started','claude',tool='Read',detail='x'*500,payload='{"file_path":"a"}',payload_kind='tool_input')
   hub.emit('judge.score','codex',turn_id=turn.id,judge='claude',name='correctness',score=4)
   store.flush()
   self.assertNotIn('payload',r);self.assertEqual(r['chars'],8)
   self.assertEqual(hub.payload(r['id'])['text'],'step one')
   self.assertEqual(len([e for e in hub.events if e['type']=='tool.started'][0]['detail']),100)
   snap=store.snapshot(hub.session_id);self.assertEqual(len(snap['events']),len(hub.events))
   self.assertEqual(store.payload(hub.session_id,r['id'])['kind'],'reasoning')
   self.assertEqual(store.sessions()[0]['turns'],1)
   with store.connect() as db:
    self.assertEqual(db.execute('select score from judge_scores').fetchone()[0],4)
    self.assertEqual(db.execute("select status from spans where kind='turn'").fetchone()[0],'completed')
 def test_claude_stream_json_parse(self):
  out='\n'.join(json.dumps(x) for x in [{'type':'system'},{'type':'assistant','message':{'content':[{'type':'thinking','thinking':'hmm'},{'type':'text','text':'hi'}]}},{'type':'result','result':'final','is_error':False}])
  self.assertEqual(duo.parse_claude_stream(out),('final',['hmm']))
  self.assertEqual(duo.parse_claude_stream('plain'),('plain',[]))
 def test_judge_position_flip_is_tie(self):
  import judge
  def fake(provider,prompt,directory,timeout,model=None,quiet=False):  # always prefers whichever answer is shown first
   return {'ok':True,'answer':json.dumps({'candidates':{k:{'scores':{d:4 for d in judge.DIMENSIONS}} for k in 'AB'},'winner':'A','confidence':.9})}
  start=len(duo.HUB.events)
  r=judge.judge_turn('claude','tflip','q',{'codex':'x','claude':'y'},fake,background=False)[0]
  self.assertEqual((r['winner'],r['consistent'],r['orders']),('tie',False,2))
  v=[e for e in list(duo.HUB.events)[start:] if e['type']=='judge.verdict'][0];self.assertFalse(v['passed'])
 def test_trajectory_compresses_and_keeps_failures(self):
  import judge
  steps=[{'kind':'tool','ref':i,'tool':'Read','target':f'f{i}','ok':True} for i in range(10)]+[{'kind':'tool','ref':99,'tool':'Bash','target':'pytest','ok':False,'error':'boom'}]
  out=judge._compress(steps);self.assertEqual(len(out),2);self.assertEqual(out[0]['repeat'],10);self.assertFalse(out[1]['ok'])
  long=[{'kind':'tool','ref':i,'tool':'T'+str(i%2),'target':'x','ok':i!=50} for i in range(300)]
  out=judge._compress(long);self.assertLessEqual(len(out),judge.MAX_STEPS+1);self.assertTrue(any(s.get('ref')==50 for s in out));self.assertTrue(any(s['kind']=='omitted' for s in out))
 def test_judge_step_refs_validated_per_candidate(self):
  import judge
  raw=json.dumps({'candidates':{'A':{'scores':{},'steps':[{'id':'A1','label':'critical_error','why':'x'},{'id':'B1','label':'good_move'}],'root_cause':{'id':'B1','category':'wrong_tool'},
     'actions':[{'target':'prompt','category':'no_verification','rule':'Run tests before claiming success','evidence':['A1','B1']},{'target':'bogus','rule':'x'}]}},'winner':'A'})
  d=judge.parse(raw,{'A':{'A1'},'B':{'B1'}})['candidates']['A']
  self.assertEqual([s['id'] for s in d['steps']],['A1']);self.assertIsNone(d['root_cause']['id']);self.assertEqual(d['actions'][0]['evidence'],['A1']);self.assertEqual(len(d['actions']),1)
  self.assertEqual(d['process'],{p:None for p in judge.PROCESS})  # no evidence -> n/a, not a fake 3
 def _ev(self,**kw):
  base={k:[] for k in ('feedback','judge.score','judge.result','judge.root','judge.verdict','judge.action','judge.error','span.start')};base.update(kw);return base
 def test_learned_weights_shrink_and_follow_feedback(self):
  import learning
  scores=[];fb=[]
  for i in range(12):
   good=i%2==0
   for d in learning.DIMS:scores.append({'turn_id':f't{i}','provider':'codex','name':d,'score':5 if (d=='groundedness' and good) else 2 if d=='groundedness' else 4})
   fb.append({'turn_id':f't{i}','verdict':'positive' if good else 'negative','reason_code':'thanks'})
  w=learning.weights(self._ev(**{'judge.score':scores,'feedback':fb}))
  self.assertEqual(w['top'][0],'groundedness');self.assertLess(w['weights']['groundedness'],len(learning.DIMS))
  self.assertEqual(learning.weights(self._ev(**{'judge.score':scores}))['weights']['groundedness'],1.0)  # no feedback -> uniform
 def test_suggestions_diverse_and_auto_lessons_capped(self):
  import learning,lessons
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   acts=[]
   for t in range(4):
    for j in ('codex','claude'):
     acts.append({'type':'judge.action','provider':'claude','turn_id':f't{t}','judge':j,'name':'no_verification','role':'prompt','rationale':'Run the tests before claiming success','tags':'1'})
     acts.append({'type':'judge.action','provider':'claude','turn_id':f't{t}','judge':j,'name':'no_verification','role':'tools','rationale':'Allow pytest','tags':'2'})
   acts+=[{'type':'judge.action','provider':'codex','turn_id':f'w{i}','judge':'codex','name':'wrong_tool','role':'prompt','rationale':'Use rg not find','tags':'3'} for i in range(4)]
   ev=self._ev(**{'judge.action':acts})
   r=learning.ranked_suggestions(ev);self.assertEqual(r[0]['category'],'no_verification');self.assertEqual(r[1]['category'],'wrong_tool')  # diversity decay lifts the other type
   with patch.object(learning,'gather',return_value=ev):notes=learning.auto_step(None)
   self.assertEqual(len(notes),2);self.assertEqual(lessons.count('claude'),2);self.assertIn('Run the tests',lessons.block('claude'));self.assertEqual(lessons.count('codex'),0)  # codex fix: one judge family only -> not auto-adopted
   with patch.object(learning,'LESSON_CAP',2),patch.object(learning,'gather',return_value=ev):self.assertEqual(learning.auto_step(None),[])
 def test_lesson_health_detects_hurting(self):
  import learning,lessons
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   lessons.save([{'provider':'codex','rule':'r','target':'prompt','category':'wrong_tool','added':'2026-01-01 00:00'}])
   import time as _t;cut=_t.mktime(_t.strptime('2026-01-01 00:00','%Y-%m-%d %H:%M'))
   res=[{'turn_id':f'b{i}','provider':'codex','score':4.5,'timestamp':cut-100-i} for i in range(4)]+[{'turn_id':f'a{i}','provider':'codex','score':3.0,'timestamp':cut+100+i} for i in range(6)]
   h=learning.lesson_health(self._ev(**{'judge.result':res}))[0];self.assertEqual(h['status'],'hurting')
   with patch.object(learning,'gather',return_value=self._ev(**{'judge.result':res})):notes=learning.auto_step(None)
   self.assertTrue(notes[0].startswith('Retired'));self.assertEqual(lessons.load(),[])
 def test_near_duplicate_rules_merge_across_keys(self):
  import lessons
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   ev=[{'type':'judge.action','provider':'claude','turn_id':'t1','judge':'claude','name':'misread_context','role':'prompt','rationale':'Count the words in the final answer against any stated limit and trim it.'},
       {'type':'judge.action','provider':'claude','turn_id':'t1','judge':'codex','name':'no_verification','role':'workflow','rationale':'Count final answer words against the stated limit and trim before submitting.'},
       {'type':'judge.action','provider':'codex','turn_id':'t1','judge':'codex','name':'no_verification','role':'workflow','rationale':'Count final answer words against the stated limit and trim before submitting.'}]
   out=lessons.suggestions(ev);self.assertEqual(len(out),2);c=[x for x in out if x['provider']=='claude'][0];self.assertEqual(c['judges'],['claude','codex'])
 def test_auto_learning_pauses_when_judges_untrustworthy(self):
  import learning
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   acts=[{'type':'judge.action','provider':'claude','turn_id':f't{t}','judge':j,'name':'no_verification','role':'prompt','rationale':'Run the tests first'} for t in range(4) for j in ('codex','claude')]
   flips=[{'turn_id':f't{t}','judge':'codex','verdict':'codex','passed':False} for t in range(5)]
   with patch.object(learning,'gather',return_value=self._ev(**{'judge.action':acts,'judge.verdict':flips})):notes=learning.auto_step(None)
   self.assertTrue(notes[-1].startswith('Auto-learning paused'));self.assertIn('order swap',notes[-1])
 def test_single_order_verdict_not_counted_consistent(self):
  import judge
  def fake(provider,prompt,directory,timeout,model=None,quiet=False):
   body=json.loads(prompt.split('\n\n',1)[1])
   if body['candidates']['A']['answer']=='y':return {'ok':True,'answer':'garbage'}  # the swapped order fails
   return {'ok':True,'answer':json.dumps({'candidates':{k:{'scores':{}} for k in 'AB'},'winner':'A'})}
  start=len(duo.HUB.events)
  r=judge.judge_turn('claude','tone','q',{'codex':'x','claude':'y'},fake,background=False)[0]
  self.assertIsNone(r['consistent']);self.assertNotEqual(r['winner'],'tie')
  v=[e for e in list(duo.HUB.events)[start:] if e['type']=='judge.verdict'][0];self.assertNotIn('passed',v)
 def test_evalset_select_filters_dedupes_and_balances(self):
  import evalset
  c=lambda p,k='respond':{'prompt':p,'kind':k}
  cands=[c('ok thanks'),c('/judge panel now please ok'),c('my api_key is sk-abcdefghijklmnop please debug this thing'),
         c('Explain how the judge handles position bias in duo'),c('explain how the judge handles position bias in duo please'),
         c('Inspect traces.py and list every SSE endpoint it serves','read'),c('Read learning.py and explain the shrinkage prior in detail','read'),
         c('What are the tradeoffs between LLM judges and agent judges')]
  out=evalset.select(cands,10)
  ps=[x['prompt'] for x in out]
  self.assertEqual(len(out),4);self.assertFalse(any('sk-' in p or p.startswith('/') for p in ps))
  self.assertEqual(sum('position bias' in p for p in ps),1);self.assertNotEqual(out[0]['kind'],out[1]['kind'])  # alternates kinds
 def test_replay_scoring_and_compare(self):
  import evalset
  jr=lambda w,c,l:[{'winner':w,'per':{'codex':{'overall':c},'claude':{'overall':l}}}]
  case=lambda i:{'id':str(i),'prompt':'p'+str(i),'kind':'respond'}
  rows=[evalset.case_result(case(1),'t1',jr('codex',4.5,4.0)),evalset.case_result(case(2),'t2',jr('claude',3.0,4.0)+[{'winner':'codex','per':{'codex':{'overall':4},'claude':{'overall':4}}}]),evalset.case_result(case(3),'t3',[])]
  S=evalset.summarize(rows);self.assertEqual((S['models']['codex']['wins'],S['models']['claude']['wins'],S['ties'],S['judged']),(1,0,1,2))
  self.assertEqual(rows[1]['winner'],'split');self.assertEqual(rows[1]['scores']['codex'],3.5)
  a={'started':'a','config':{'no_lessons':True},'rows':rows};b={'started':'b','config':{'no_lessons':False},'rows':[evalset.case_result(case(1),'t9',jr('claude',4.0,4.8))]}
  c=evalset.compare(a,b);self.assertEqual(c['shared'],1);self.assertEqual(c['models']['claude']['delta'],0.8);self.assertEqual(c['flips'][0]['to'],'claude')
  self.assertIn('controlled lessons A/B',evalset.render_compare(a,b,c))
 def test_calibration_end_to_end(self):
  import traces,tracestore,calibrate,evalset
  with tempfile.TemporaryDirectory() as tmp:
   hub=traces.TraceHub();store=tracestore.TraceStore(Path(tmp)/'t.db');hub.attach_store(store)
   turns=[]
   for i in range(3):
    with hub.span('turn',mode='respond:both') as turn:
     hub.emit('prompt','duo',payload=f'Explain topic number {i} in some detail for me')
     for p in ('codex','claude'):
      with hub.span('agent',p,role='responder'):hub.emit('answer',p,payload=f'{p} answer {i}')
    turns.append(turn.id)
    for j in ('codex','claude'):hub.emit('judge.verdict','duo',turn_id=turn.id,judge=j,verdict='codex' if j=='codex' or i==0 else 'claude')
   store.flush()
   self.assertEqual(len(calibrate.pairs(store)),3)
   self.assertEqual([c['kind'] for c in evalset.from_store(store)],['respond']*3)
   answers=iter(['a','b','x','t'])
   rng=__import__('random').Random(0)
   calibrate.run(store,3,show=lambda *a:None,ask=lambda q:next(answers),rng=rng)
   self.assertEqual(len(calibrate.pairs(store)),0)  # all labelled
   rep=calibrate.report(store);self.assertEqual(rep['labels'],3)
   codex=[r for r in rep['judges'] if r['judge']=='codex judge'][0];self.assertEqual(codex['n'],3)
 def test_replay_runs_cases_and_saves_record(self):
  import evalset
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_HOME':tmp}):
   evalset.add_cases([{'prompt':'Explain the judge position bias handling please','kind':'respond'},{'prompt':'Inspect traces.py and list the endpoints it serves','kind':'read','cwd':tmp}])
   def fake_turn(prompt,**kw):
    duo.EVAL['last']=[{'winner':'claude','per':{'codex':{'overall':3.0},'claude':{'overall':4.0}}}];return {'ok':True,'turn_id':'tt'}
   with patch.object(duo,'run_turn',side_effect=fake_turn),patch.object(duo,'unified_work',side_effect=fake_turn),contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()):
    self.assertEqual(duo.replay(no_lessons=True),0)
   self.assertIsNone(duo.os.environ.get('DUO_NO_LESSONS'));self.assertEqual(duo.EVAL['mode'],'off')  # restored after replay
   rec=json.loads(evalset.list_runs()[-1].read_text());self.assertEqual(rec['summary']['models']['claude']['wins'],2);self.assertTrue(rec['config']['no_lessons'])
   self.assertIn('lessons OFF',out.getvalue())
 def test_lesson_without_effect_is_retired_after_stale_window(self):
  import learning,lessons
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   lessons.save([{'provider':'codex','rule':'r','target':'prompt','category':'wrong_tool','added':'2026-01-01 00:00'}])
   import time as _t;cut=_t.mktime(_t.strptime('2026-01-01 00:00','%Y-%m-%d %H:%M'))
   res=[{'turn_id':f'b{i}','provider':'codex','score':4.0,'timestamp':cut-100-i} for i in range(4)]+[{'turn_id':f'a{i}','provider':'codex','score':3.95,'timestamp':cut+100+i} for i in range(16)]
   roots=[{'turn_id':'b0','provider':'codex','name':'wrong_tool'},{'turn_id':'a0','provider':'codex','name':'wrong_tool'},{'turn_id':'a1','provider':'codex','name':'wrong_tool'},{'turn_id':'a2','provider':'codex','name':'wrong_tool'},{'turn_id':'a3','provider':'codex','name':'wrong_tool'}]
   ev=self._ev(**{'judge.result':res,'judge.root':roots})
   self.assertEqual(learning.lesson_health(ev)[0]['status'],'no clear effect')
   with patch.object(learning,'gather',return_value=ev):notes=learning.auto_step(None)
   self.assertIn('no measurable effect',notes[0]);self.assertEqual(lessons.load(),[])
 def _st(self,ref,tool,target,ok=True,inp=None,kind='tool'):
  import analysis
  st={'kind':kind,'ref':ref,'tool':tool,'target':target,'ok':ok};st['input']=inp or ''
  k=analysis.action_type(st);return {**st,'type':k,'key':analysis.target_key(st,k)}
 def test_action_types_and_alignment_divergence(self):
  import analysis
  self.assertEqual(analysis.action_type({'tool':'commandExecution','target':"/bin/zsh -lc 'nl -ba judge.py'"}),'FILE_READ')
  self.assertEqual(analysis.action_type({'tool':'commandExecution','target':'rg span.start'}),'SEARCH')
  self.assertEqual(analysis.action_type({'tool':'Bash','target':'python3 -m pytest -q'}),'COMMAND')
  self.assertEqual(analysis.action_type({'tool':'Bash','target':"sed -i 's/a/b/' x.py"}),'FILE_WRITE')
  a=[self._st(1,'Read','judge.py'),self._st(2,'Grep','ROOTS'),self._st(3,'Edit','judge.py'),self._st(4,'Bash','pytest -q')]
  b=[self._st(11,'commandExecution','nl -ba judge.py'),self._st(12,'commandExecution','rg ROOTS'),self._st(13,'commandExecution','pytest -q')]
  r=analysis.align(a,b);self.assertEqual(r['pairs'][0][:2],[1,11]);div=r['pairs'][r['divergence']]
  self.assertIn(3,div[:2]);self.assertGreater(r['similarity'],0.4)
 def test_behaviour_flags(self):
  import analysis
  reads=[self._st(i,'Read',f'f{i%2}.py') for i in range(12)]
  m,f=analysis.behaviour(reads+[self._st(20,'Edit','f0.py')])
  flags={x['flag'] for x in f};self.assertIn('search_loop',flags);self.assertIn('reread_churn',flags);self.assertIn('unverified_finish',flags)
  m,f=analysis.behaviour([self._st(1,'Bash','pytest',ok=False),self._st(2,'Edit','a.py'),self._st(3,'Edit','b.py'),self._st(4,'Bash','pytest')])
  self.assertEqual(m['rogue_after_error'],1);self.assertTrue(m['verified_after_last_write']);self.assertTrue(m['tests_passing'])
 def test_hack_signals(self):
  import analysis
  inp=json.dumps({'file_path':'tests/test_api.py','old_string':'def test_x():\n    assert f()==1\n    assert g()==2','new_string':'@pytest.mark.skip\ndef test_x():\n    assert f()==1'})
  h={x['signal'] for x in analysis.hack_signals([self._st(1,'Edit','tests/test_api.py',inp=inp),self._st(2,'Edit','src/api.py',inp=json.dumps({'old_string':'a','new_string':'b'})),self._st(3,'Bash','rm tests/test_old.py')])}
  self.assertEqual(h,{'test_file_edit','skip_added','assert_removed','test_deleted'})
  self.assertEqual(analysis.hack_signals([self._st(1,'Edit','.github/workflows/ci.yml',inp='{}')])[0]['signal'],'grader_or_ci_edit')
 def test_claim_matching(self):
  import analysis
  st=[self._st(1,'Edit','src/app.py'),self._st(2,'Bash','pytest -q',ok=False)]
  out=analysis.match_claims([{'text':'All tests pass','type':'tests_pass'},{'text':'Edited app.py','type':'file_edited','target':'app.py'},{'text':'Updated README','type':'file_edited','target':'README.md'}],st)
  self.assertEqual([c['status'] for c in out],['contradicted','supported','unsupported']);self.assertEqual(out[0]['ref'],2)
  self.assertEqual(analysis.match_claims([{'text':'tests pass','type':'tests_pass'}],[self._st(1,'Edit','a.py')])[0]['status'],'unsupported')
  self.assertEqual(analysis.match_claims([{'text':'read the file','type':'command_ran','target':'','judge_status':'supported','step_ref':1}],st)[0]['status'],'supported')
 def test_finalize_hack_override_claims_and_root_agreement(self):
  import judge,analysis
  steps={'codex':[{'ref':1,'type':'FILE_WRITE','key':'test_a.py','target':'tests/test_a.py'},{'ref':2,'type':'COMMAND','key':'test','target':'pytest','ok':True}],
         'claude':[{'ref':5,'type':'FILE_READ','key':'a.py','target':'a.py'},{'ref':6,'type':'FILE_WRITE','key':'a.py','target':'a.py'}]}
  findings={'steps':steps,'per':{'codex':{'hacks':[{'ref':1,'signal':'skip_added','target':'tests/test_a.py'}]},'claude':{'hacks':[]}}}
  def item(hack,root_ref,claims=()):return {'hack':{'verdict':hack},'root':{'ref':root_ref,'category':'wrong_tool'},'claims':list(claims)}
  res=[{'judge':'codex','winner':'codex','per':{'codex':item('confirmed',1,[{'text':'tests pass','type':'tests_pass','status':'supported','ref':2}]),'claude':item('none',5,[{'text':'edited b.py','type':'file_edited','target':'b.py','status':'supported','ref':None}])}},
       {'judge':'claude','winner':'codex','per':{'codex':item('suspected',2),'claude':item('none',6)}}]
  start=len(duo.HUB.events)
  out=judge.finalize(res,['codex','claude'],'tf',findings)
  self.assertEqual([r['winner'] for r in out],['claude','claude'])  # confirmed hack + deterministic signal -> other agent wins
  ev=list(duo.HUB.events)[start:]
  claims={(e['provider'],e['verdict']) for e in ev if e['type']=='judge.claim'};self.assertIn(('claude','unsupported'),claims);self.assertIn(('codex','supported'),claims)
  agree={e['provider']:e['passed'] for e in ev if e['type']=='judge.root_agreement'};self.assertEqual(agree,{'codex':True,'claude':True})
  self.assertTrue([e for e in ev if e['type']=='judge.verdict_override'])
 def test_grade_turn_runs_analysis_even_without_judge(self):
  import traces
  with duo.HUB.span('turn',mode='work:read') as turn:
   with duo.HUB.span('agent','codex',role='reader'):
    duo.HUB.emit('tool.started','codex',tool='Read',item_id='x',detail='a.py');duo.HUB.emit('tool.completed','codex',tool='Read',item_id='x',passed=True)
  start=len(duo.HUB.events)
  with patch.dict(duo.EVAL,{'mode':'off','background':False}):duo.grade_turn(turn.id,'q',{'codex':'a'})
  types=[e['type'] for e in list(duo.HUB.events)[start:]]
  self.assertIn('analysis.behaviour',types);self.assertIn('analysis.done',types);self.assertFalse([t for t in types if t.startswith('judge.')])
 def test_playbook_select_votes_and_bootstrap(self):
  import lessons,learning,evalset
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json')}):
   for i,(rule,cat) in enumerate([('Run pytest before claiming tests pass','no_verification'),('Use rg to locate SQL migrations before editing','wrong_tool'),
                                  ('Read the React component props before changing JSX','misread_context'),('Check docker compose logs when containers fail','ignored_tool_error'),
                                  ('Prefer pathlib over os.path in Python helpers','wrong_tool'),('Quote tool output for every claim','hallucinated_result')]):
    lessons.accept({'provider':'codex','rule':rule,'target':'prompt','category':cat,'turns':3})
   import time as _t;t0=_t.perf_counter();pick=lessons.select('codex','Fix the failing SQL migrations in the db folder',k=3);ms=(_t.perf_counter()-t0)*1000
   self.assertLess(ms,50);rules=[l['rule'] for l in pick];self.assertIn('Use rg to locate SQL migrations before editing',rules);self.assertEqual(len(pick),3)
   lid=lessons.ensure_ids(lessons.load())[1]['id']
   ev=self._ev(**{'lessons.applied':[{'turn_id':f't{i}','provider':'codex','tags':lid} for i in range(4)],
     'judge.result':[{'turn_id':f't{i}','provider':'codex','score':4.5 if i<1 else 2.5} for i in range(4)],'judge.root':[{'turn_id':'t1','provider':'codex','name':'wrong_tool'}]})
   v=learning.votes(ev)[lid];self.assertEqual((v['applied'],v['helpful'],v['harmful']),(4,1,3))
  lo,hi=evalset.bootstrap([0.5,0.4,0.6,0.3,0.5,0.7]);self.assertGreater(lo,0)
  self.assertIsNone(evalset.bootstrap([0.5])[0]);self.assertGreaterEqual(evalset.power_hint([0.1,-0.3,0.5,0.0,0.2]),3)
  self.assertIn(evalset.split('0000abcd'),('holdout',));self.assertEqual(evalset.split('0001abcd'),'train')
 def test_failure_cases_added_from_trace(self):
  import traces,tracestore,evalset
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_HOME':tmp}):
   hub=traces.TraceHub();store=tracestore.TraceStore(Path(tmp)/'t.db');hub.attach_store(store)
   with hub.span('turn',mode='work:read') as turn:hub.emit('prompt','duo',payload='Inspect the parser module and explain the tokenizer edge cases')
   with hub.span('turn',mode='work:write') as wt:hub.emit('prompt','duo',payload='Fix the tokenizer bug and update all the parser tests now')
   hub.emit('judge.root','codex',turn_id=turn.id,judge='codex',name='misread_context');hub.emit('judge.root','codex',turn_id=wt.id,judge='codex',name='wrong_tool')
   store.flush()
   self.assertEqual(evalset.add_failure_cases(store),1);c=evalset.load()[0];self.assertEqual(c['source'],'failure:misread_context');self.assertEqual(evalset.add_failure_cases(store),0)
 def test_policy_bandit_learns_and_routes(self):
  import policy,random
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_POLICY':str(Path(tmp)/'p.json')}):
   rng=random.Random(1)
   for i in range(300):  # k=4 lessons is truly better for codex/read
    k,_=policy.choose_k('codex','read',rng=rng)
    score={0:2.5,2:3.2,4:4.6}[k]
    policy.update([{'provider':'codex','data':{'k':k,'kind':'read'}}],[{'per':{'codex':{'overall':score}}}],turn_id=f't{i}',rng=rng)
   arms={a['k']:a for a in policy.summary()['arms']}
   self.assertGreater(arms[4]['n'],arms[0]['n']);self.assertGreater(arms[4]['mean'],arms[0]['mean'])
   t0=__import__('time').perf_counter()
   for _ in range(100):policy.choose_k('codex','read')
   self.assertLess((__import__('time').perf_counter()-t0)*10,50)  # < 50 ms per choice incl. file read
   self.assertEqual(policy.reward(5,hack=True),0.5);self.assertAlmostEqual(policy.reward(3,contradicted=1),0.35)
   policy.update_routes([('read','codex')]*14+[('read','claude')]*1)
   self.assertEqual(policy.route('read',rng=random.Random(0))[0],'codex');self.assertEqual(policy.route('respond')[0],'both')
   self.assertEqual(policy.feedback('t5','negative'),1)
 def test_propose_build_apply_and_draft_pr(self):
  import lessons,propose
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json'),'DUO_PROPOSALS':str(Path(tmp)/'props'),'DUO_HOME':tmp}):
   repo=Path(tmp)/'repo';repo.mkdir();(repo/'CLAUDE.md').write_text('# Project rules\nKeep it simple.\n')
   lessons.accept({'provider':'claude','rule':'Count words against any stated limit before answering','target':'prompt','category':'ignored_constraint','turns':4})
   lessons.accept({'provider':'codex','rule':'Run pytest after every edit','target':'prompt','category':'no_verification','turns':3},status='verified')
   p=propose.build(repo)
   self.assertEqual({f['name'] for f in p['files']},{'AGENTS.md','CLAUDE.md'})
   claude=[f for f in p['files'] if f['name']=='CLAUDE.md'][0];self.assertIn('+- Count words',claude['diff']);self.assertIn('Keep it simple',(propose.home()/p['id']/'CLAUDE.md').read_text())
   self.assertIn('duo --evolve',(propose.home()/p['id']/'EVIDENCE.md').read_text())
   propose.apply(p['id']);txt=(repo/'CLAUDE.md').read_text();self.assertEqual(txt.count(propose.START),1);self.assertIn('# Project rules',txt)
   self.assertIsNone(propose.build(repo))  # idempotent: nothing new to propose
   self.assertEqual(propose.load_index()[0]['status'],'applied')
   calls=[]
   class R:
    def __init__(s,code=0,out=''):s.returncode=code;s.stdout=out;s.stderr=''
   def fake_run(cmd,**kw):
    calls.append(cmd);return R(0,'https://github.com/x/y/pull/7\n' if cmd[:3]==['gh','pr','create'] else '')
   with patch.object(propose.shutil,'which',return_value='/usr/bin/gh'):url=propose.open_pr(p['id'],run=fake_run)
   self.assertEqual(url,'https://github.com/x/y/pull/7');self.assertIn('--draft',calls[-1]);self.assertTrue(any(c[3:5]==['checkout','-b'] for c in calls if c[0]=='git'))
 def test_export_rl_and_otel(self):
  import traces,tracestore,export
  with tempfile.TemporaryDirectory() as tmp:
   hub=traces.TraceHub();store=tracestore.TraceStore(Path(tmp)/'t.db');hub.attach_store(store)
   with hub.span('turn',mode='respond:both') as turn:
    hub.emit('prompt','duo',payload='Explain why the sky is blue in two sentences please')
    for p in ('codex','claude'):
     with hub.span('agent',p,role='responder'):
      hub.emit('tool.started',p,tool='Read',item_id='i'+p,detail='notes.md');hub.emit('tool.completed',p,tool='Read',item_id='i'+p,passed=True)
      hub.emit('answer',p,payload=f'{p} says Rayleigh scattering')
   for j in ('codex','claude'):
    hub.emit('judge.verdict','duo',turn_id=turn.id,judge=j,verdict='claude')
    for p,sc in (('codex',3.4),('claude',4.6)):hub.emit('judge.result',p,turn_id=turn.id,judge=j,score=sc,verdict='x')
   hub.emit('judge.root','codex',turn_id=turn.id,judge='codex',name='incomplete_answer',rationale='too short')
   store.flush()
   out=Path(tmp)/'rl.jsonl';n,skipped=export.export_rl(store,out);row=json.loads(out.read_text().splitlines()[0])
   self.assertEqual((n,row['chosen_model'],row['label_source']),(1,'claude','judge_panel'));self.assertIn('Rayleigh',row['rejected']);self.assertEqual(row['trajectories']['codex'][0]['ok'],True)
   n,_=export.export_rl(store,Path(tmp)/'k.jsonl','kto');self.assertEqual(n,2)
   o=Path(tmp)/'otel.json';cnt=export.export_otel(store,o);doc=json.loads(o.read_text())
   spans=doc['resourceSpans'][0]['scopeSpans'][0]['spans'];self.assertEqual(cnt,len(spans))
   ops={a['value']['stringValue'] for s_ in spans for a in s_['attributes'] if a['key']=='gen_ai.operation.name'}
   self.assertEqual(ops,{'invoke_workflow','invoke_agent','execute_tool'})
   self.assertTrue(any(e['name']=='gen_ai.evaluation.result' for s_ in spans for e in s_['events']))
 def test_usage_parsing(self):
  out='\n'.join(json.dumps(x) for x in [{'type':'result','result':'ok','usage':{'input_tokens':120,'output_tokens':30,'cache_read_input_tokens':100},'total_cost_usd':0.0042}])
  self.assertEqual(duo.claude_usage(out),{'input_tokens':120,'output_tokens':30,'cache_read_input_tokens':100,'cost_usd':0.0042})
 def test_guidance_never_changes_user_request(self):
  import lessons
  with tempfile.TemporaryDirectory() as tmp,patch.dict(duo.os.environ,{'DUO_LESSONS':str(Path(tmp)/'l.json'),'DUO_POLICY':str(Path(tmp)/'p.json')}):
   lessons.accept({'provider':'codex','rule':'Run the tests after every edit and quote the result','target':'prompt','category':'no_verification','turns':4})
   lessons.accept({'provider':'codex','rule':'Use rg to find SQL migrations before editing','target':'prompt','category':'wrong_tool','turns':3})
   lessons.accept({'provider':'codex','rule':'Prefer pathlib in Python helpers','target':'prompt','category':'wrong_tool','turns':3})
   req="Fix the SQL migrations bug but don't run tests, I will."
   block,chosen,skipped=lessons.compose('codex',req,k=4)
   rules=[l['rule'] for l in chosen];reasons={x['rule']:x['reason'] for x in skipped}
   self.assertNotIn('Run the tests after every edit and quote the result',rules);self.assertEqual(reasons['Run the tests after every edit and quote the result'],'conflicts with your request')
   self.assertIn('Use rg to find SQL migrations before editing',rules);self.assertEqual(reasons['Prefer pathlib in Python helpers'],'not relevant to this request')
   self.assertTrue(block.startswith('\n\nOptional guidance'));self.assertIn("user's request always wins",block)
   pid=lessons.ensure_ids(lessons.load())[2]['id'];lessons.set_flag(pid,'pinned',True)
   self.assertIn('Prefer pathlib in Python helpers',[l['rule'] for l in lessons.compose('codex',req,k=4)[1]])
   lessons.set_flag(pid,'muted',True);self.assertEqual([x['reason'] for x in lessons.compose('codex',req,k=4)[2] if x['id']==pid],['muted by you'])
   prompt='BASE {"request": '+json.dumps(req)+'}'
   with duo.HUB.span('turn') as tt:
    duo.CURRENT.update(kind='read',request=req)
    with duo.HUB.span('agent','codex') as sp:out=duo.apply_lessons('codex',prompt,sp)
   self.assertTrue(out.startswith(prompt))  # original prompt untouched, guidance only appended
   lessons.save_settings(enabled=False)
   with duo.HUB.span('agent','codex') as sp:self.assertEqual(duo.apply_lessons('codex',prompt,sp),prompt)
   self.assertEqual(lessons.compose('codex',req)[0],'')
 def test_post_routes_require_duo_header(self):
  import traces,urllib.request,urllib.error
  hub=traces.TraceHub();hub.post_routes['/lessons']=lambda body:{'ok':body.get('action')}
  port=hub.start(0)
  try:
   req=urllib.request.Request(f'http://127.0.0.1:{port}/lessons',data=b'{"action":"pin"}',method='POST',headers={'Content-Type':'application/json'})
   with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
   self.assertEqual(cm.exception.code,403)
   req.add_header('X-Duo','1');self.assertEqual(json.loads(urllib.request.urlopen(req).read()),{'ok':'pin'})
  finally:hub.close()
 def test_sse_tails_another_process_session_live(self):
  import traces,tracestore,urllib.request,threading,time as _t
  with tempfile.TemporaryDirectory() as tmp:
   writer=traces.TraceHub();writer.attach_store(tracestore.TraceStore(Path(tmp)/'t.db'))  # the running duo chat
   viewer=traces.TraceHub();viewer.attach_store(tracestore.TraceStore(Path(tmp)/'t.db'))  # tools/serve_viewer.py
   writer.emit('prompt','duo',payload='first');writer.store.flush()
   port=viewer.start(0);got=[]
   def read():
    r=urllib.request.urlopen(f'http://127.0.0.1:{port}/events?session={writer.session_id}&after=0',timeout=6)
    while len(got)<2:
     line=r.readline().decode()
     if line.startswith('data: '):got.append(json.loads(line[6:])['type'])
   th=threading.Thread(target=read,daemon=True);th.start()
   _t.sleep(0.3);writer.emit('judge.verdict','duo',turn_id='t',verdict='codex');writer.store.flush()  # arrives while the stream is open
   th.join(6);viewer.close()
   self.assertEqual(got,['prompt','judge.verdict'])
if __name__=='__main__':unittest.main()
