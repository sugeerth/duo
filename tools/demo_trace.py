#!/usr/bin/env python3
"""Write a synthetic demo session to a trace DB so the viewer can be previewed without API keys.

    python3 tools/demo_trace.py /tmp/duo-demo.db
    DUO_TRACE_DB=/tmp/duo-demo.db python3 tools/serve_viewer.py

Every prompt, tool call, answer, timing and judge score below is made up (seeded, so reruns match).
The README screenshots come from this data.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracestore

R = random.Random(7)
OUTCOME = ['correctness', 'completeness', 'groundedness', 'instruction_following', 'efficiency']
PROCESS = ['planning', 'tool_selection', 'error_recovery', 'verification']
MODELS = ('codex', 'claude')
START = 1792058400.0  # fixed clock so screenshots are stable

# (mode, prompt, per-model tool plan, per-model quality tweaks). A tool tuple ending in True fails.
TURNS = [
    ('work:read', 'Where is the retry/backoff logic, and does it cap the delay?',
     {'codex': [('commandExecution', 'rg -n "retry|backoff" src'), ('commandExecution', 'sed -n 1,90p src/net/retry.py'),
                ('commandExecution', 'rg -n "max_delay" src tests'), ('commandExecution', 'python -m pytest -q tests/test_retry.py')],
      'claude': [('Grep', 'retry|backoff'), ('Read', 'src/net/backoff.py', True), ('Glob', 'src/net/*.py'), ('Read', 'src/net/retry.py'),
                 ('Grep', 'max_delay'), ('Read', 'tests/test_retry.py')]},
     {'codex': {'verification': .8}, 'claude': {'error_recovery': .6}}),
    ('respond:both', 'In under 100 words: when should I use a semaphore instead of a mutex?', None,
     {'claude': {'instruction_following': -2.0, 'completeness': .6}, 'codex': {'instruction_following': .4}}),
    ('work:read', 'Why does test_parse_path fail on Windows-style paths?',
     {'codex': [('commandExecution', 'python -m pytest -q tests/test_paths.py -k parse_path'), ('commandExecution', 'sed -n 1,60p src/paths.py'),
                ('commandExecution', 'rg -n "split\\(\'/\'\\)" src')],
      'claude': [('Read', 'tests/test_paths.py'), ('Read', 'src/paths.py'), ('Grep', "split\\('/'\\)"), ('Read', 'src/cli.py')]},
     {'claude': {'correctness': .7, 'groundedness': .5}}),
    ('work:read', 'List every config option that defaults to an unsafe value.',
     {'codex': [('commandExecution', 'rg -n "DEFAULTS|default=" src/config'), ('commandExecution', 'sed -n 1,140p src/config/schema.py'),
                ('commandExecution', 'rg -n "verify_tls|allow_http|debug" src', True), ('commandExecution', 'rg -n "verify_tls|allow_http|debug" src/'),
                ('commandExecution', 'cat docs/config.md')],
      'claude': [('Glob', 'src/config/**/*.py'), ('Read', 'src/config/schema.py'), ('Grep', 'verify_tls|allow_http|debug'),
                 ('Read', 'src/config/loader.py'), ('Read', 'docs/config.md'), ('Grep', 'os.environ.get')]},
     {'claude': {'completeness': .8}, 'codex': {'completeness': -.5}}),
    ('respond:both', 'Draft a three-sentence release note for v0.4 from CHANGELOG.md.', None,
     {'codex': {'correctness': .5, 'instruction_following': .5}}),
    ('work:read', 'Which public functions in src/ have no tests?',
     {'codex': [('commandExecution', 'rg -n "^def [a-z]" src'), ('commandExecution', 'rg -n "import|from src" tests'),
                ('commandExecution', 'python -m pytest -q --co')],
      'claude': [('Grep', '^def [a-z]'), ('Glob', 'tests/test_*.py'), ('Read', 'tests/test_cli.py'), ('Read', 'tests/test_export.py'),
                 ('Grep', 'def test_')]},
     {'codex': {'efficiency': .6}, 'claude': {'efficiency': -.6, 'completeness': .4}}),
]
# Made-up tendencies: Codex tighter on instructions and speed, Claude more thorough.
BIAS = {'codex': {'instruction_following': .5, 'efficiency': .5, 'completeness': -.2},
        'claude': {'completeness': .4, 'groundedness': .3, 'efficiency': -.4}}
FIXES = {'no_verification': 'Run the relevant test before saying it passes.',
         'missing_context': 'Read the docs the code points to before answering.',
         'premature_stop': 'Check every file the search matched before concluding.',
         'misread_context': 'Answer the exact scope asked, not a broader one.',
         'ignored_constraint': 'Respect explicit word limits.'}
ROOTS = ['no_verification', 'missing_context', 'premature_stop', 'misread_context']


def new_id():
    return '%012x' % R.getrandbits(48)


def clamp(x):
    return max(1.0, min(5.0, round(x * 2) / 2))


class Session:
    def __init__(self, store):
        self.store, self.sid, self.n = store, new_id(), 0
        store.session(self.sid, START, '/demo/acme-cli')

    def put(self, events):
        """Emit events in time order; returns {key: event id} for events that carried a key."""
        ids = {}
        for e in sorted(events, key=lambda e: e['ts']):
            self.n += 1
            payload, kind, key = e.pop('payload', None), e.pop('kind', None), e.pop('key', None)
            event = {'session_id': self.sid, 'id': self.n, 'timestamp': e.pop('ts'), **e}
            if payload:
                event.update(chars=len(payload), payload_kind=kind)
            self.store.event(event, payload, kind)
            if key:
                ids[key] = self.n
        return ids


def agent(ev, provider, role, phase, parent, turn_id, t, tools, answer):
    span = new_id()
    ev += [dict(ts=t, type='span.start', provider=provider, span_id=span, parent_id=parent, span_kind='agent', role=role),
           dict(ts=t, type='agent.started', provider=provider, span_id=span, phase=phase)]
    if role != 'synthesis':
        ev += [dict(ts=t + .01, type='policy.arm', provider=provider, span_id=span, turn_id=turn_id,
                    data={'k': 2, 'kind': 'thompson', 'explored': False, 'manual': False}),
               dict(ts=t + .01, type='lessons.applied', provider=provider, span_id=span, turn_id=turn_id, count=1, tags='',
                    duration_ms=2.1, data={'skipped': []}, kind='lessons_block',
                    payload='Optional guidance (the request always wins):\n1. Respect explicit length limits.')]
    clock, keys, fails = t + R.uniform(1.0, 2.5), [], 0
    for i, (tool, detail, *bad) in enumerate(tools or []):
        failed = bool(bad and bad[0])
        slow = 'pytest' in detail
        dur = R.uniform(2.5, 5.0) if slow else (R.uniform(.3, 1.8) if provider == 'codex' else R.uniform(.05, .6))
        item, key = f'{span}-{i}', (span, i)
        if R.random() < .45:
            ev.append(dict(ts=clock - .3, type='reasoning', provider=provider, span_id=span, item_id=f'rs-{item}'))
        ev += [dict(ts=clock, type='tool.started', provider=provider, span_id=span, tool=tool, item_id=item, detail=detail, key=key,
                    payload=json.dumps({'command' if provider == 'codex' else 'input': detail}), kind='tool_input'),
               dict(ts=clock + dur, type='tool.completed', provider=provider, span_id=span, tool=tool, item_id=item,
                    status='failed' if failed else 'completed', passed=not failed, kind='tool_output',
                    payload='No such file or directory' if failed else f'(demo output for: {detail})')]
        keys.append(key)
        fails += failed
        clock += dur + R.uniform(.6, 3.0)
    end = clock + R.uniform(3, 8)
    ms = int((end - t) * 1000)
    out = R.randint(400, 900)
    ev.append(dict(ts=end, type='usage', provider=provider, span_id=span,
                   data={'input_tokens': R.randint(2, 40), 'output_tokens': out, 'cache_read_input_tokens': R.randint(500, 4000),
                         'cache_creation_input_tokens': R.randint(1000, 5000), 'cost_usd': round(out * 6e-5, 4)}))
    if role == 'reader':
        ev += [dict(ts=end, type='agent.completed', provider=provider, span_id=span, status='completed', duration_ms=ms,
                    payload=answer, kind='answer'),
               dict(ts=end, type='evaluation', provider=provider, span_id=span, name='backend_success', passed=True)]
        if tools:
            ev.append(dict(ts=end, type='evaluation', provider=provider, span_id=span, name='tool_reliability', passed=fails == 0,
                           score=round(1 - fails / len(tools), 2), tools=len(tools), tool_errors=fails))
    else:
        ev += [dict(ts=end, type='agent.completed', provider=provider, span_id=span, status='completed', duration_ms=ms),
               dict(ts=end, type='evaluation', provider=provider, span_id=span, name='nonempty_successful_response', passed=True),
               dict(ts=end, type='answer', provider=provider, span_id=span, payload=answer, kind='answer')]
    ev.append(dict(ts=end, type='span.end', provider=provider, span_id=span, parent_id=parent, span_kind='agent', status='completed',
                   duration_ms=ms, tools=len(tools or []), tool_errors=fails, approvals=0, declined=0, role=role))
    return end, keys, fails


def run_turn(S, t0, mode, prompt, plan):
    turn_id, ev = new_id(), []
    ev += [dict(ts=t0, type='span.start', provider='duo', span_id=turn_id, parent_id=None, span_kind='turn', mode=mode),
           dict(ts=t0, type='prompt', provider='duo', span_id=turn_id, payload=prompt, kind='prompt')]
    reading = mode.startswith('work')
    role, phase = ('reader', 'review') if reading else ('responder', 'response')
    ends, keys, fails = {}, {}, {}
    for p in ('claude', 'codex'):
        ends[p], keys[p], fails[p] = agent(ev, p, role, phase, turn_id, turn_id, t0 + R.uniform(0, .05), (plan or {}).get(p),
                                           f'(demo {p} answer to: {prompt})')
    synth_end, _, _ = agent(ev, 'claude', 'synthesis', 'response', turn_id, turn_id, max(ends.values()) + .05, None,
                            f'(demo combined answer to: {prompt})')
    ms = int((synth_end - t0) * 1000)
    if reading:
        ev.append(dict(ts=synth_end, type='workflow.completed', provider='duo', span_id=turn_id, duration_ms=ms, status='completed'))
    ev.append(dict(ts=synth_end, type='span.end', provider='duo', span_id=turn_id, parent_id=None, span_kind='turn', status='completed',
                   duration_ms=ms, tools=0, tool_errors=0, approvals=0, declined=0, mode=mode))
    ids = S.put(ev)
    return turn_id, synth_end, {p: [ids[k] for k in keys[p]] for p in MODELS}, fails, ends


def judge_turn(S, turn_id, t, tool_ids, fails, tweaks):
    ev, base = [], t + .01
    dims = OUTCOME + (PROCESS if any(tool_ids.values()) else [])
    truth = {p: {d: 3.6 + BIAS[p].get(d, 0) + tweaks.get(p, {}).get(d, 0) - .5 * fails[p] * (d == 'efficiency') + R.uniform(-.4, .4)
                 for d in dims} for p in MODELS}
    for p in MODELS:
        n = len(tool_ids[p])
        ev.append(dict(ts=base, type='analysis.behaviour', provider=p, turn_id=turn_id,
                       data={'steps': n, 'writes': 0, 'failures': fails[p], 'longest_read_streak': n, 'reread_files': 0, 'oscillations': 0,
                             'rogue_after_error': 0, 'tests_run': 0, 'verified_after_last_write': None, 'tests_passing': False,
                             'explore_share': 1.0 if n else 0.0, 'reasoning_per_call': 0, 'flags': []}))
    if tool_ids['codex'] and tool_ids['claude']:
        pairs = [list(x) for x in zip(tool_ids['codex'], tool_ids['claude'])]
        ev.append(dict(ts=base, type='analysis.align', provider='duo', turn_id=turn_id,
                       data={'pairs': pairs, 'divergence': abs(len(tool_ids['codex']) - len(tool_ids['claude'])),
                             'similarity': round(R.uniform(.35, .7), 3)}))
    ev.append(dict(ts=base, type='analysis.done', provider='duo', turn_id=turn_id, duration_ms=0))
    winners = {}
    for j in MODELS:
        done = base
        for a in MODELS:
            span = new_id()
            dur = R.uniform(14, 19) if j == 'claude' else R.uniform(34, 45)
            done = max(done, base + dur)
            ev += [dict(ts=base, type='span.start', provider=j, span_id=span, parent_id=None, span_kind='judge', role='judge',
                        turn_id=turn_id, mode=f'{j} · A={a}'),
                   dict(ts=base + dur, type='judge.raw', provider=j, span_id=span, turn_id=turn_id, judge=j, kind='judge_output',
                        payload=json.dumps({'demo': True, 'order': f'A={a}'})),
                   dict(ts=base + dur, type='span.end', provider=j, span_id=span, parent_id=None, span_kind='judge', status='completed',
                        duration_ms=int(dur * 1000), tools=0, tool_errors=0, approvals=0, declined=0, role='judge', turn_id=turn_id,
                        mode=f'{j} · A={a}')]
        means = {}
        for p in MODELS:
            scores = {d: clamp(truth[p][d] + (.3 if p == j else 0) + R.uniform(-.35, .35)) for d in dims}
            means[p] = round(sum(scores.values()) / len(scores), 2)
            ev += [dict(ts=done, type='judge.score', provider=p, turn_id=turn_id, judge=j, name=d, score=s) for d, s in scores.items()]
            for i, item in enumerate(tool_ids[p]):
                ev.append(dict(ts=done, type='judge.step', provider=p, turn_id=turn_id, judge=j, item_id=str(item),
                               verdict='good_move' if R.random() > .2 else 'wasted_step', rationale='(demo step label)'))
            ev.append(dict(ts=done, type='judge.claim', provider=p, turn_id=turn_id, judge=j, name='fact',
                           verdict='supported' if R.random() > .25 else 'unsupported',
                           item_id=str(tool_ids[p][0]) if tool_ids[p] else '', rationale='(demo claim check)'))
        diff = means['codex'] - means['claude']
        winner = 'tie' if abs(diff) < .25 else ('codex' if diff > 0 else 'claude')
        winners[j] = winner
        for p in MODELS:
            other = means[[m for m in MODELS if m != p][0]]
            verdict = 'tie' if winner == 'tie' else ('win' if winner == p else 'loss')
            ev.append(dict(ts=done, type='judge.result', provider=p, turn_id=turn_id, judge=j, score=means[p], passed=means[p] >= 3.5,
                           verdict=verdict, tags='', rationale='(demo rationale)', role='llm'))
            if means[p] < other:
                root = 'ignored_constraint' if truth[p]['instruction_following'] < 2.5 else R.choice(ROOTS)
                ev += [dict(ts=done, type='judge.root', provider=p, turn_id=turn_id, judge=j, item_id='', name=root, rationale=FIXES[root], passed=True),
                       dict(ts=done, type='judge.action', provider=p, turn_id=turn_id, judge=j, name=root, role='prompt', rationale=FIXES[root], tags='')]
        ev.append(dict(ts=done, type='judge.verdict', provider='duo', turn_id=turn_id, judge=j, verdict=winner,
                       confidence=round(min(.95, .5 + abs(diff) / 2), 2), count=2, passed=winner != 'tie', rationale='(demo)'))
    last = max(e['ts'] for e in ev)
    ev += [dict(ts=last, type='judge.root_agreement', provider='claude', turn_id=turn_id, passed=winners['codex'] == winners['claude']),
           dict(ts=last, type='judge.agreement', provider='duo', turn_id=turn_id, passed=winners['codex'] == winners['claude'])]
    ev += [dict(ts=last, type='policy.reward', provider=p, turn_id=turn_id,
                score=round(sum(truth[p].values()) / len(truth[p]) / 5, 3)) for p in MODELS]
    S.put(ev)
    return last


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/duo-demo.db'
    store = tracestore.TraceStore(path)
    S, t = Session(store), START
    for mode, prompt, plan, tweaks in TURNS:
        turn_id, end, tool_ids, fails, _ = run_turn(S, t, mode, prompt, plan)
        t = judge_turn(S, turn_id, end, tool_ids, fails, tweaks or {}) + R.uniform(25, 60)
    store.flush(10)
    print(f'demo session {S.sid}: {len(TURNS)} turns, {S.n} events -> {path}')


if __name__ == '__main__':
    main()
