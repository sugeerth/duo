#!/usr/bin/env python3
"""Write a synthetic demo session to a trace DB so the viewer can be previewed without API keys.

    python3 tools/demo_trace.py /tmp/duo-demo.db
    DUO_TRACE_DB=/tmp/duo-demo.db python3 tools/serve_viewer.py

Every prompt, tool call, answer, timing and judge score below is made up (seeded, so reruns match).
The README screenshots come from this data.

Layout: the demo is data (TURNS). Each builder function returns plain event dicts for one concern
(an agent run, its tool calls, one judge's scores...). Only Session.put writes to the store.
"""
import json
import os
import random
import sys
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tracestore

MODELS = ('codex', 'claude')
OUTCOME = ['correctness', 'completeness', 'groundedness', 'instruction_following', 'efficiency']
PROCESS = ['planning', 'tool_selection', 'error_recovery', 'verification']
START = 1792058400.0      # fixed clock so screenshots are stable
SEED = 7
BASE_QUALITY = 3.6        # hidden "true" score before per-model bias and per-turn tweaks
SELF_PREFERENCE = .3      # each judge leans toward its own model
TIE_MARGIN = .25          # mean-score gap below which a judge calls a tie
R = random.Random(SEED)


class Tool(NamedTuple):
    name: str
    detail: str
    fails: bool = False


class Turn(NamedTuple):
    mode: str
    prompt: str
    tools: dict    # model -> [Tool]; empty for answer-only turns
    tweaks: dict   # model -> {dimension: score shift}


def shell(cmd, fails=False):
    return Tool('commandExecution', cmd, fails)


TURNS = [
    Turn('work:read', 'Where is the retry/backoff logic, and does it cap the delay?',
         {'codex': [shell('rg -n "retry|backoff" src'), shell('sed -n 1,90p src/net/retry.py'),
                    shell('rg -n "max_delay" src tests'), shell('python -m pytest -q tests/test_retry.py')],
          'claude': [Tool('Grep', 'retry|backoff'), Tool('Read', 'src/net/backoff.py', fails=True), Tool('Glob', 'src/net/*.py'),
                     Tool('Read', 'src/net/retry.py'), Tool('Grep', 'max_delay'), Tool('Read', 'tests/test_retry.py')]},
         {'codex': {'verification': .8}, 'claude': {'error_recovery': .6}}),
    Turn('respond:both', 'In under 100 words: when should I use a semaphore instead of a mutex?', {},
         {'claude': {'instruction_following': -2.0, 'completeness': .6}, 'codex': {'instruction_following': .4}}),
    Turn('work:read', 'Why does test_parse_path fail on Windows-style paths?',
         {'codex': [shell('python -m pytest -q tests/test_paths.py -k parse_path'), shell('sed -n 1,60p src/paths.py'),
                    shell('rg -n "split\\(\'/\'\\)" src')],
          'claude': [Tool('Read', 'tests/test_paths.py'), Tool('Read', 'src/paths.py'), Tool('Grep', "split\\('/'\\)"),
                     Tool('Read', 'src/cli.py')]},
         {'claude': {'correctness': .7, 'groundedness': .5}}),
    Turn('work:read', 'List every config option that defaults to an unsafe value.',
         {'codex': [shell('rg -n "DEFAULTS|default=" src/config'), shell('sed -n 1,140p src/config/schema.py'),
                    shell('rg -n "verify_tls|allow_http|debug" src', fails=True), shell('rg -n "verify_tls|allow_http|debug" src/'),
                    shell('cat docs/config.md')],
          'claude': [Tool('Glob', 'src/config/**/*.py'), Tool('Read', 'src/config/schema.py'), Tool('Grep', 'verify_tls|allow_http|debug'),
                     Tool('Read', 'src/config/loader.py'), Tool('Read', 'docs/config.md'), Tool('Grep', 'os.environ.get')]},
         {'claude': {'completeness': .8}, 'codex': {'completeness': -.5}}),
    Turn('respond:both', 'Draft a three-sentence release note for v0.4 from CHANGELOG.md.', {},
         {'codex': {'correctness': .5, 'instruction_following': .5}}),
    Turn('work:read', 'Which public functions in src/ have no tests?',
         {'codex': [shell('rg -n "^def [a-z]" src'), shell('rg -n "import|from src" tests'), shell('python -m pytest -q --co')],
          'claude': [Tool('Grep', '^def [a-z]'), Tool('Glob', 'tests/test_*.py'), Tool('Read', 'tests/test_cli.py'),
                     Tool('Read', 'tests/test_export.py'), Tool('Grep', 'def test_')]},
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


def event(ts, etype, provider, **fields):
    """A trace event. Optional `payload`/`kind` hold stored text; optional `key` lets callers look up its id."""
    return {'ts': ts, 'type': etype, 'provider': provider, **fields}


class Session:
    """The only writer: numbers events in time order and hands them to the trace store."""
    META = ('ts', 'payload', 'kind', 'key')

    def __init__(self, store):
        self.store, self.sid, self.n = store, new_id(), 0
        store.session(self.sid, START, '/demo/acme-cli')

    def put(self, events):
        """Emit events in time order; returns {key: event id} for events that carried a key."""
        ids = {}
        for e in sorted(events, key=lambda e: e['ts']):
            self.n += 1
            record = {'session_id': self.sid, 'id': self.n, 'timestamp': e['ts'],
                      **{k: v for k, v in e.items() if k not in self.META}}
            if e.get('payload'):
                record.update(chars=len(e['payload']), payload_kind=e['kind'])
            self.store.event(record, e.get('payload'), e.get('kind'))
            if e.get('key'):
                ids[e['key']] = self.n
        return ids


# --- one agent run ---------------------------------------------------------------------------

def agent_start(provider, role, phase, turn_id, span, t):
    ev = [event(t, 'span.start', provider, span_id=span, parent_id=turn_id, span_kind='agent', role=role),
          event(t, 'agent.started', provider, span_id=span, phase=phase)]
    if role != 'synthesis':
        ev += [event(t + .01, 'policy.arm', provider, span_id=span, turn_id=turn_id,
                     data={'k': 2, 'kind': 'thompson', 'explored': False, 'manual': False}),
               event(t + .01, 'lessons.applied', provider, span_id=span, turn_id=turn_id, count=1, tags='',
                     duration_ms=2.1, data={'skipped': []}, kind='lessons_block',
                     payload='Optional guidance (the request always wins):\n1. Respect explicit length limits.')]
    return ev


def tool_duration(provider, tool):
    if 'pytest' in tool.detail:
        return R.uniform(2.5, 5.0)
    return R.uniform(.3, 1.8) if provider == 'codex' else R.uniform(.05, .6)


def tool_calls(provider, span, clock, tools):
    """Started/completed pairs with think-time gaps. Returns (events, clock after last call, keys, failures)."""
    ev, keys, fails = [], [], 0
    for i, tool in enumerate(tools):
        dur = tool_duration(provider, tool)
        item, key = f'{span}-{i}', (span, i)
        if R.random() < .45:
            ev.append(event(clock - .3, 'reasoning', provider, span_id=span, item_id=f'rs-{item}'))
        ev += [event(clock, 'tool.started', provider, span_id=span, tool=tool.name, item_id=item, detail=tool.detail, key=key,
                     payload=json.dumps({'command' if provider == 'codex' else 'input': tool.detail}), kind='tool_input'),
               event(clock + dur, 'tool.completed', provider, span_id=span, tool=tool.name, item_id=item,
                     status='failed' if tool.fails else 'completed', passed=not tool.fails, kind='tool_output',
                     payload='No such file or directory' if tool.fails else f'(demo output for: {tool.detail})')]
        keys.append(key)
        fails += tool.fails
        clock += dur + R.uniform(.6, 3.0)
    return ev, clock, keys, fails


def agent_finish(provider, role, span, turn_id, t, end, n_tools, fails, answer):
    ms = int((end - t) * 1000)
    out = R.randint(400, 900)
    ev = [event(end, 'usage', provider, span_id=span,
                data={'input_tokens': R.randint(2, 40), 'output_tokens': out, 'cache_read_input_tokens': R.randint(500, 4000),
                      'cache_creation_input_tokens': R.randint(1000, 5000), 'cost_usd': round(out * 6e-5, 4)})]
    if role == 'reader':  # tool runs carry the answer on completion, like the real backends
        ev += [event(end, 'agent.completed', provider, span_id=span, status='completed', duration_ms=ms, payload=answer, kind='answer'),
               event(end, 'evaluation', provider, span_id=span, name='backend_success', passed=True)]
        if n_tools:
            ev.append(event(end, 'evaluation', provider, span_id=span, name='tool_reliability', passed=fails == 0,
                            score=round(1 - fails / n_tools, 2), tools=n_tools, tool_errors=fails))
    else:
        ev += [event(end, 'agent.completed', provider, span_id=span, status='completed', duration_ms=ms),
               event(end, 'evaluation', provider, span_id=span, name='nonempty_successful_response', passed=True),
               event(end, 'answer', provider, span_id=span, payload=answer, kind='answer')]
    ev.append(event(end, 'span.end', provider, span_id=span, parent_id=turn_id, span_kind='agent', status='completed',
                    duration_ms=ms, tools=n_tools, tool_errors=fails, approvals=0, declined=0, role=role))
    return ev


def agent_run(provider, role, phase, turn_id, t, tools, answer):
    """One agent span. Returns (events, end time, tool-call keys, failure count)."""
    span = new_id()
    ev = agent_start(provider, role, phase, turn_id, span, t)
    calls, clock, keys, fails = tool_calls(provider, span, t + R.uniform(1.0, 2.5), tools)
    end = clock + R.uniform(3, 8)
    return ev + calls + agent_finish(provider, role, span, turn_id, t, end, len(tools), fails, answer), end, keys, fails


# --- one turn: both agents, then a synthesis ---------------------------------------------------

def run_turn(S, t0, turn):
    """Write one turn; returns (turn id, end time, tool event ids per model, failures per model)."""
    turn_id = new_id()
    reading = turn.mode.startswith('work')
    role, phase = ('reader', 'review') if reading else ('responder', 'response')
    ev = [event(t0, 'span.start', 'duo', span_id=turn_id, parent_id=None, span_kind='turn', mode=turn.mode),
          event(t0, 'prompt', 'duo', span_id=turn_id, payload=turn.prompt, kind='prompt')]
    ends, keys, fails = {}, {}, {}
    for p in ('claude', 'codex'):
        run, ends[p], keys[p], fails[p] = agent_run(p, role, phase, turn_id, t0 + R.uniform(0, .05), turn.tools.get(p, []),
                                                   f'(demo {p} answer to: {turn.prompt})')
        ev += run
    synth, end, _, _ = agent_run('claude', 'synthesis', 'response', turn_id, max(ends.values()) + .05, [],
                                 f'(demo combined answer to: {turn.prompt})')
    ms = int((end - t0) * 1000)
    ev += synth
    if reading:
        ev.append(event(end, 'workflow.completed', 'duo', span_id=turn_id, duration_ms=ms, status='completed'))
    ev.append(event(end, 'span.end', 'duo', span_id=turn_id, parent_id=None, span_kind='turn', status='completed',
                    duration_ms=ms, tools=0, tool_errors=0, approvals=0, declined=0, mode=turn.mode))
    ids = S.put(ev)
    return turn_id, end, {p: [ids[k] for k in keys[p]] for p in MODELS}, fails


# --- after the turn: analysis, the blind judge panel, rewards ------------------------------------

def true_quality(dims, fails, tweaks):
    """Hidden per-model quality; judges see it through noise and self-preference."""
    return {p: {d: BASE_QUALITY + BIAS[p].get(d, 0) + tweaks.get(p, {}).get(d, 0) - .5 * fails[p] * (d == 'efficiency')
                + R.uniform(-.4, .4) for d in dims} for p in MODELS}


def analysis_events(turn_id, t, tool_ids, fails):
    ev = []
    for p in MODELS:
        n = len(tool_ids[p])
        ev.append(event(t, 'analysis.behaviour', p, turn_id=turn_id,
                        data={'steps': n, 'writes': 0, 'failures': fails[p], 'longest_read_streak': n, 'reread_files': 0,
                              'oscillations': 0, 'rogue_after_error': 0, 'tests_run': 0, 'verified_after_last_write': None,
                              'tests_passing': False, 'explore_share': 1.0 if n else 0.0, 'reasoning_per_call': 0, 'flags': []}))
    if tool_ids['codex'] and tool_ids['claude']:
        ev.append(event(t, 'analysis.align', 'duo', turn_id=turn_id,
                        data={'pairs': [list(x) for x in zip(tool_ids['codex'], tool_ids['claude'])],
                              'divergence': abs(len(tool_ids['codex']) - len(tool_ids['claude'])),
                              'similarity': round(R.uniform(.35, .7), 3)}))
    ev.append(event(t, 'analysis.done', 'duo', turn_id=turn_id, duration_ms=0))
    return ev


def judge_spans(judge, turn_id, t):
    """The judge grades the pair twice, once per A/B order. Returns (events, time both finished)."""
    ev, done = [], t
    for a in MODELS:
        span, mode = new_id(), f'{judge} · A={a}'
        dur = R.uniform(14, 19) if judge == 'claude' else R.uniform(34, 45)
        done = max(done, t + dur)
        ev += [event(t, 'span.start', judge, span_id=span, parent_id=None, span_kind='judge', role='judge', turn_id=turn_id, mode=mode),
               event(t + dur, 'judge.raw', judge, span_id=span, turn_id=turn_id, judge=judge, kind='judge_output',
                     payload=json.dumps({'demo': True, 'order': f'A={a}'})),
               event(t + dur, 'span.end', judge, span_id=span, parent_id=None, span_kind='judge', status='completed',
                     duration_ms=int(dur * 1000), tools=0, tool_errors=0, approvals=0, declined=0, role='judge',
                     turn_id=turn_id, mode=mode)]
    return ev, done


def judge_scores(judge, turn_id, t, dims, truth, tool_ids):
    """Per-dimension scores, step labels and one claim check for each model. Returns (events, mean score per model)."""
    ev, means = [], {}
    for p in MODELS:
        lean = SELF_PREFERENCE if p == judge else 0
        scores = {d: clamp(truth[p][d] + lean + R.uniform(-.35, .35)) for d in dims}
        means[p] = round(sum(scores.values()) / len(scores), 2)
        ev += [event(t, 'judge.score', p, turn_id=turn_id, judge=judge, name=d, score=s) for d, s in scores.items()]
        ev += [event(t, 'judge.step', p, turn_id=turn_id, judge=judge, item_id=str(item),
                     verdict='good_move' if R.random() > .2 else 'wasted_step', rationale='(demo step label)') for item in tool_ids[p]]
        ev.append(event(t, 'judge.claim', p, turn_id=turn_id, judge=judge, name='fact',
                        verdict='supported' if R.random() > .25 else 'unsupported',
                        item_id=str(tool_ids[p][0]) if tool_ids[p] else '', rationale='(demo claim check)'))
    return ev, means


def judge_results(judge, turn_id, t, means, truth):
    """Win/loss per model, a root cause and fix for the lower score, then the verdict. Returns (events, winner)."""
    diff = means['codex'] - means['claude']
    winner = 'tie' if abs(diff) < TIE_MARGIN else ('codex' if diff > 0 else 'claude')
    ev = []
    for p in MODELS:
        other = means['claude' if p == 'codex' else 'codex']
        verdict = 'tie' if winner == 'tie' else ('win' if winner == p else 'loss')
        ev.append(event(t, 'judge.result', p, turn_id=turn_id, judge=judge, score=means[p], passed=means[p] >= 3.5,
                        verdict=verdict, tags='', rationale='(demo rationale)', role='llm'))
        if means[p] < other:
            root = 'ignored_constraint' if truth[p]['instruction_following'] < 2.5 else R.choice(ROOTS)
            ev += [event(t, 'judge.root', p, turn_id=turn_id, judge=judge, item_id='', name=root, rationale=FIXES[root], passed=True),
                   event(t, 'judge.action', p, turn_id=turn_id, judge=judge, name=root, role='prompt', rationale=FIXES[root], tags='')]
    ev.append(event(t, 'judge.verdict', 'duo', turn_id=turn_id, judge=judge, verdict=winner,
                    confidence=round(min(.95, .5 + abs(diff) / 2), 2), count=2, passed=winner != 'tie', rationale='(demo)'))
    return ev, winner


def panel_outcome(turn_id, t, winners, truth):
    agree = winners['codex'] == winners['claude']
    return ([event(t, 'judge.root_agreement', 'claude', turn_id=turn_id, passed=agree),
             event(t, 'judge.agreement', 'duo', turn_id=turn_id, passed=agree)]
            + [event(t, 'policy.reward', p, turn_id=turn_id, score=round(sum(truth[p].values()) / len(truth[p]) / 5, 3))
               for p in MODELS])


def judge_turn(S, turn_id, t, tool_ids, fails, tweaks):
    """Write the post-turn pipeline; returns when the last judge finished."""
    start = t + .01
    dims = OUTCOME + (PROCESS if any(tool_ids.values()) else [])
    truth = true_quality(dims, fails, tweaks)
    ev, winners = analysis_events(turn_id, start, tool_ids, fails), {}
    for judge in MODELS:
        spans, done = judge_spans(judge, turn_id, start)
        scores, means = judge_scores(judge, turn_id, done, dims, truth, tool_ids)
        results, winners[judge] = judge_results(judge, turn_id, done, means, truth)
        ev += spans + scores + results
    last = max(e['ts'] for e in ev)
    S.put(ev + panel_outcome(turn_id, last, winners, truth))
    return last


def build(path, seed=SEED):
    """Write the whole demo session to `path`; returns (session id, event count)."""
    R.seed(seed)
    store = tracestore.TraceStore(path)
    S, t = Session(store), START
    for turn in TURNS:
        turn_id, end, tool_ids, fails = run_turn(S, t, turn)
        t = judge_turn(S, turn_id, end, tool_ids, fails, turn.tweaks) + R.uniform(25, 60)
    store.flush(10)
    return S.sid, S.n


if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/duo-demo.db'
    sid, n = build(out)
    print(f'demo session {sid}: {len(TURNS)} turns, {n} events -> {out}')
