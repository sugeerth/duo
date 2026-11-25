"""Generate FILE_MAP.html: a one-screen map of AgentLens's files, what each holds, and how data flows.
Line counts and new/changed status are read from disk (baseline = the Oct 3 morning backups)."""
from pathlib import Path
import html
import re

ROOT = Path(__file__).resolve().parent.parent
BASE = [Path.home() / 'tools' / 'duo_backup_20261003', Path.home() / 'tools' / 'duo_backup_viewer_20261003']

# stage, file, one-line role, key functions/contents
FILES = [
    ('Capture', 'agentlens.py', 'CLI: runs both agents, adds lessons, triggers analysis', ['run_turn / unified_work', 'apply_lessons (hot path, <1 ms)', 'grade_turn → analysis + judges', 'replay · evolve · fork · propose flags']),
    ('Capture', 'agents.py', 'Drives Codex + Claude; records tools, reasoning, usage', ['codex_run / claude_run', 'tool input/output payloads', 'thinking + token usage']),
    ('Record', 'traces.py', 'Live event hub + loopback HTTP server for the viewer', ['span tree: session→turn→run→tool', '/events SSE · /snapshot · /payload', 'pluggable JSON routes']),
    ('Record', 'tracestore.py', 'SQLite trace DB (~/.agentlens/traces.db)', ['sessions · spans · events', 'payloads (reasoning, tool I/O)', 'judge_scores table']),
    ('Understand', 'analysis.py', 'Deterministic trace analysis, no model calls', ['align(): where the runs diverge', 'behaviour flags (loops, no verify)', 'cheat detector · claim matcher']),
    ('Understand', 'judge.py', 'LLM/agent judges read trajectories blind, both orders', ['outcome + process scores', 'A2P root cause + minimal fix', 'claims · conflicts · reward-hack override']),
    ('Learn', 'learning.py', 'Self-learning kept in check', ['feedback → what matters to you', 'votes · auto-lessons · retire', 'health checks (gates learning)']),
    ('Learn', 'lessons.py', 'Playbook of rules per agent (~/.agentlens/lessons.json)', ['top-k selection per request', 'merge near-duplicates', '/improve suggestions']),
    ('Learn', 'policy.py', 'RL on top of the agents: Thompson-sampling bandit', ['arm = lessons injected (0/2/4)', 'reward = judge − cheats − false claims', 'routing posterior per task type']),
    ('Learn', 'evalset.py', 'Fixed eval set, replay, held-out split, paired stats', ['--evalset · --replay · --evolve', 'failure → regression cases', 'bootstrap CI · power hint']),
    ('Learn', 'calibrate.py', 'Checks the judges against your blind labels', ['--label (A/B blind)', '--calibration agreement + CI']),
    ('Act', 'propose.py', 'Agent PRs: lessons into AGENTS.md / CLAUDE.md', ['managed section + evidence', 'versioned in ~/.agentlens/proposals', '--apply · --pr (draft PR)']),
    ('Act', 'export.py', 'Data out for training and other tools', ['DPO / KTO preference JSONL', 'OpenTelemetry GenAI OTLP/JSON']),
    ('See', 'viewer.html', 'One-page viewer, linked by the selected turn', ['verdict · learning curve · RL policy', 'film strip · phase flow · timeline', 'said vs did · fingerprint · PR board']),
]
STAGES = ['Capture', 'Record', 'Understand', 'Learn', 'Act', 'See']
STAGE_NOTE = {'Capture': 'run the agents', 'Record': 'store every step', 'Understand': 'read the trace', 'Learn': 'decide what helps', 'Act': 'change the agents', 'See': 'show it'}

def lines(p):
    try: return sum(1 for _ in p.open())
    except OSError: return 0

def status(name):
    for b in BASE:
        old = b / name
        if old.exists():
            return 'changed' if old.read_bytes() != (ROOT / name).read_bytes() else 'same'
    return 'new'

def build():
    rows = [(st, f, role, keys, lines(ROOT / f), status(f)) for st, f, role, keys in FILES]
    total = sum(r[4] for r in rows); new = sum(r[5] == 'new' for r in rows); changed = sum(r[5] == 'changed' for r in rows)
    maxl = max(r[4] for r in rows)
    colw, cardh, gap, top, left = 200, 92, 12, 74, 16
    by = {s: [r for r in rows if r[0] == s] for s in STAGES}
    H = top + max(len(v) for v in by.values()) * (cardh + gap) + 70
    W = left * 2 + len(STAGES) * colw + (len(STAGES) - 1) * 26
    svg = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="AgentLens file map">',
           '<defs><marker id="ar" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8Z" fill="var(--muted)"/></marker></defs>']
    centers = {}
    for i, st in enumerate(STAGES):
        x = left + i * (colw + 26)
        svg.append(f'<text x="{x}" y="22" class="stage">{i + 1}. {st}</text><text x="{x}" y="40" class="note">{STAGE_NOTE[st]}</text>')
        for j, (s, f, role, keys, n, stt) in enumerate(by[st]):
            y = top + j * (cardh + gap)
            tip = html.escape(f'{f} · {n} lines · {stt}\n' + role + '\n• ' + '\n• '.join(keys))
            words = role.split(); l1 = ''; l2 = ''
            for w in words:
                if len(l1 + ' ' + w) <= 30 and not l2: l1 = (l1 + ' ' + w).strip()
                else: l2 = (l2 + ' ' + w).strip()
            if len(l2) > 31: l2 = l2[:30] + '…'
            badge = {'new': ('NEW', 'var(--new)'), 'changed': ('CHANGED', 'var(--chg)'), 'same': ('', 'none')}[stt]
            bar = max(3, (colw - 96) * n / maxl)  # leave room for the line-count label
            svg.append(f'<g class="card" tabindex="0"><title>{tip}</title><rect x="{x}" y="{y}" width="{colw}" height="{cardh}" rx="8" class="box"/>'
                       f'<text x="{x + 12}" y="{y + 22}" class="fname">{f}</text>'
                       + (f'<rect x="{x + colw - 12 - len(badge[0]) * 7.2 - 10}" y="{y + 10}" width="{len(badge[0]) * 7.2 + 10}" height="17" rx="8.5" fill="{badge[1]}"/><text x="{x + colw - 12 - (len(badge[0]) * 7.2 + 10) / 2}" y="{y + 22.5}" text-anchor="middle" class="badge">{badge[0]}</text>' if badge[0] else '')
                       + f'<text x="{x + 12}" y="{y + 42}" class="role">{html.escape(l1)}</text><text x="{x + 12}" y="{y + 57}" class="role">{html.escape(l2)}</text>'
                       f'<rect x="{x + 12}" y="{y + 70}" width="{bar:.1f}" height="6" rx="3" class="bar"/><text x="{x + colw - 12}" y="{y + 77}" text-anchor="end" class="lines">{n} lines</text></g>')
        centers[st] = (x, x + colw)
    ymid = top + cardh / 2
    for a, b in zip(STAGES, STAGES[1:]):
        svg.append(f'<line x1="{centers[a][1] + 3}" y1="{ymid}" x2="{centers[b][0] - 4}" y2="{ymid}" stroke="var(--muted)" stroke-width="2" marker-end="url(#ar)"/>')
    ly = H - 30; x1 = (centers['Learn'][0] + centers['Learn'][1]) / 2; x0 = (centers['Capture'][0] + centers['Capture'][1]) / 2
    svg.append(f'<path d="M{x1},{H - 62} L{x1},{ly} L{x0},{ly} L{x0},{H - 62}" fill="none" stroke="var(--loop)" stroke-width="2.5" marker-end="url(#ar)"/>'
               f'<text x="{(x0 + x1) / 2}" y="{ly - 7}" text-anchor="middle" class="loop">loop: best lessons + bandit arm go into the next prompt (&lt;1 ms)</text>')
    svg.append('</svg>')
    data = ['~/.agentlens/traces.db — every event, span, payload, score', '~/.agentlens/lessons.json — the playbook', '~/.agentlens/policy.json — bandit posteriors',
            '~/.agentlens/evalset.json + replays/ — fixed tasks and scored runs', '~/.agentlens/proposals/ — agent PRs (local git)']
    title = f'{len(rows)} files, {new} new and {changed} changed since Oct 3: the agents are run, recorded, understood, improved and shown'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AgentLens File Map</title>
<style>
:root{{--bg:#f9f9f7;--panel:#fcfcfb;--line:#e1e0d9;--text:#0b0b0b;--text2:#52514e;--muted:#898781;--new:#0ca30c;--chg:#2a78d6;--loop:#eb6834;--bar:#c3c2b7}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0d0d0d;--panel:#1a1a19;--line:#2c2c2a;--text:#ffffff;--text2:#c3c2b7;--muted:#898781;--chg:#3987e5;--loop:#d95926;--bar:#383835}}}}
:root[data-theme="dark"]{{--bg:#0d0d0d;--panel:#1a1a19;--line:#2c2c2a;--text:#ffffff;--text2:#c3c2b7;--muted:#898781;--chg:#3987e5;--loop:#d95926;--bar:#383835}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1360px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:19px;margin:0 0 4px}} p{{margin:0 0 14px;color:var(--text2)}}
.wrap{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;overflow-x:auto}}
svg text{{fill:var(--text2)}} .stage{{font-size:14px;font-weight:700;fill:var(--text)}} .note{{font-size:12px;fill:var(--muted)}}
.box{{fill:var(--panel);stroke:var(--line);stroke-width:1.2}} .card:hover .box,.card:focus .box{{stroke:var(--text2);stroke-width:2}}
.fname{{font:600 13px ui-monospace,SFMono-Regular,Menlo,monospace;fill:var(--text)}} .role{{font-size:11.5px}} .lines{{font-size:11px;fill:var(--muted)}}
.badge{{font-size:10px;font-weight:700;fill:#fff;letter-spacing:.03em}} .bar{{fill:var(--bar)}} .loop{{font-size:12px;fill:var(--loop);font-weight:600}}
ul{{margin:12px 0 0;padding-left:18px;color:var(--text2);font-size:13px;columns:2;column-gap:28px}} code{{font:12px ui-monospace,Menlo,monospace}}
@media (max-width:700px){{ul{{columns:1}}}}
</style></head><body><main>
<h1>{title}</h1>
<p>Hover a card for its key functions. Bar = file size ({total:,} lines in total). <span style="color:var(--new);font-weight:600">NEW</span> = created since Oct 3 · <span style="color:var(--chg);font-weight:600">CHANGED</span> = edited.</p>
<div class="wrap">{"".join(svg)}</div>
<ul>{"".join(f"<li><code>{html.escape(d.split(' — ')[0])}</code> — {html.escape(d.split(' — ')[1])}</li>" for d in data)}</ul>
</main></body></html>'''

if __name__ == '__main__':
    out = ROOT / 'FILE_MAP.html'; out.write_text(build()); print(out)
