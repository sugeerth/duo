# Duo — unified Codex + Claude terminal

Run `duo` and ask normally. Duo never hands you into either native CLI.

- Advice: both generate independent responses in parallel; Duo selects or combines.
- Read/inspect/research tasks: both tool-enabled agents run in parallel inside Duo.
- Change requests (including redesign/improve/make better): one tool-enabled writer implements, a read-only reviewer checks, and the writer makes one repair pass if confirmed blocking issues are found. Duo then synthesizes actual results.

## Setup

Needs Python 3.12+ and the `codex` and `claude` CLIs on `PATH`, already logged in (Duo reuses their auth and config).

```sh
pip install --target vendor claude-agent-sdk   # Claude agent backend; vendor/ is gitignored
pip install rich prompt_toolkit                 # optional terminal UI
ln -s "$PWD/duo.py" ~/.local/bin/duo
```

Commands: `/both`, `/separate`, `/fast` (Codex response only), `/claude` (Claude response only), `/agent codex`, `/agent claude` (single tool-enabled backend inside Duo), `/work TASK`, `/context PATH`, `/clear`, `/traces`, `/help`, `/exit`. `/auto` aliases `/both`. Explicit single-response modes do not execute tasks; restore `/both` for automatic work routing.

One-shot: `duo "question"`, `duo --work "task"`, `duo --respond "question"`, `duo --agent codex "task"`, `duo --separate "question"`. `--timeout` and model overrides apply to tool workflows too.

Tool execution uses Codex app-server and Claude Agent SDK, with existing authentication and user/project configuration. Duo hosts approval requests; it does not bypass permissions. Codex uses a workspace-write sandbox for implementation and read-only sandbox for reviews. Claude uses its default permission mode for implementation; reviewers have file/search tools without shell/edit tools. This preserves approval controls but is not identical permission behavior across providers. Unsupported structured MCP consent requests are declined.

Codex threads are ephemeral and Claude session persistence is disabled. Duo writes no response/transcript reports; traces (including payloads) go to the local trace DB described below. Follow-up context is kept in memory for the last 10 turns. Providers can retain operational logs according to their own settings. Agent-created task artifacts are written when the user asks for changes.

Optional auto-context: set `DUO_AUTO_CONTEXT=/path/to/board.html` and prompts matching `DUO_AUTO_CONTEXT_MATCH` (default: "command center") automatically include that file's visible text, excluding scripts/styles and any `id="older-reference"` section. Off when unset. Disable with --no-auto-context or /autocontext off. Explicit context and relevant history are sent to the providers used for the turn.

Terminal UI: answer panels, Markdown, spinners, response times, Tab command completion, Alt+Enter newline, Enter sends, Ctrl+D exit. Plain fallback when rich/prompt_toolkit are unavailable.

Latency: no preliminary model-routing call; read-only agents start together. First completed result appears immediately. Implementation stays sequential to avoid competing writers. Synthesis is concise and uses the faster prior responder as a heuristic. `/separate` skips synthesis; single response modes use one call. These choices do not guarantee quality or speed.

Use **Exit tracing** in the viewer header to disconnect live updates while keeping the last trace available. **Resume tracing** reconnects; the Duo task continues running. Use `/exit` in the terminal to quit Duo.

The focused view opens with a **verdict card** for the timeline's turn (an idea taken from AgentDiff). It has up to five lines, and each one restates evidence already in the trace:
- VERDICT: who won and the scores.
- CAUSE: the root-cause step, as a chip that opens that exact call. Without a root cause, the first step where the two runs diverged.
- COST: the difference in time, tokens and tool calls, and whether the cheaper side won or lost.
- FIX: the judges' minimal fix.
- CONF: low or medium, never high, because one turn is a single observation. The root cause is marked a hypothesis until `duo --fork` reruns verify or refute it.

A line with nothing to say is left out. Under the timeline, **Where the time went** puts both models' run time on one scale, split into useful tool calls, model time between calls, and wasted time (failed calls, or repeats of a call that already succeeded). It ranks tools by seconds and wasted seconds; click a tool to open its first call. "Copy report" includes the card.

**Tree view (default).** Each model's run grows as a tree on a shared start line, with idle stretches pinched. Branches are tool calls colored by phase, and the canopy is the judge score. It also draws the analysis:
- bark bands under the trunk show each burst's outcome: progress, loop, failing or quiet;
- an amber vine wraps the trunk where the run looped, and a shaded band marks the longest stall;
- tests grow as fruit: green ✓ or red ✗, larger when a check newly passes;
- failed or repeated calls are withered (dashed) branches;
- a ring and "root cause" mark the judges' root-cause call, and a thinner ring marks a critical error;
- a ▼ pin marks the "look first" step, and a dashed graft line joins the two trunks at their first divergence;
- on long runs, nearby calls of the same tool merge into one thicker ×N branch.

Every mark opens its evidence. Railway shows the real wall-clock overlap instead.

Long runs and long-term change (`longrun.py`, `longterm.py`; rules from AgentDiff's long-run reading):
- **Long run** (per turn, deterministic, after each turn): each model's calls split into sessions (idle ≥ 30 min) and bursts. The burst gap is set by the run itself: Otsu's split of its log gaps, clamped to 20 s to 30 min.
  - Progress is a check (test/lint/typecheck) newly passing or passing more cases. A regression is a check failing after it passed.
  - The stall is the longest stretch without progress. A loop is two or more bursts that each repeat at least 60% of earlier calls without progress.
  - The block draws the run on one line, with idle compressed, and gives a **Look first** step.
- **Over time** (every session in the trace DB): per-model trends in judge score, time per turn, tool failure rate and calls.
  - Tests: Mann-Kendall and Theil-Sen over turn order, the last 10 turns vs everything before (Mann-Whitney), and head-to-head drift.
  - All tests in a response are corrected with Benjamini-Hochberg. A change is stated only at q < 0.05 with n ≥ 8 per model; otherwise the block says no clear change.
  - It also shows rolling-median small multiples, a weekday × hour grid, and long-run habits (how often each model loops, and its median stall). Served at `/longterm`.

The viewer defaults to one focused view: paired model summaries combined with execution tracks. **Explore charts** reveals the full observatory and earlier detailed panels; **Focus timeline** returns to the concise view.

The viewer opens with execution tracks on a shared elapsed-time axis. Agent runs are long bars; tool calls branch off and rejoin like railway tracks. Nearby repeated successful calls cluster at overview scale; zoom up to 8× to separate them. Overlapping calls use separate tracks. Reasoning and evaluation evidence remain clickable. Select a turn, follow the latest task, or replay and scrub elapsed time. Below the tracks, a **step diff** aligns the two models' tool sequences (Needleman–Wunsch over tool + phase) and pins the first divergence in plain language — gray links join identical steps, amber marks a different tool at the same position, dashed boxes have no counterpart in the other run; every step opens its evidence. The summary cards show where each model's time went (tools vs thinking, summed tool time so overlaps can overstate it) and the slowest call. Tool-less lanes (judges, pure-answer runs) stay compact.

The **Model observatory** embeds eleven D3 charts: speed/quality atlas, phase braid, tool-busy heatmap, tool-duration/count treemap, latency dots, parallel rubric contours, next-action-after-error ribbons, directed tool-transition chords, cumulative tool-start curves, a paired outcome matrix, and an aggregated dual-model agent flow graph drawn with Sugiyama-style layering (temporal cycle breaking, longest-path layers, barycenter crossing reduction): split-pill nodes show each model's visit share, arrowed per-model edges count transitions, dashed curves mark loops back to an earlier phase, ↺ arcs count immediate repeats, dashed outlines mark single-model tools, and hovering a node isolates its traffic. Scope charts to the filtered session or timeline turn and select either model. Marks open recorded run, tool, or judge evidence. Empty states preserve missing data; chart notes explain normalization, sampling caps, and interpretation. Charts use vendored D3 and work offline. Reference links discuss Langfuse aggregated/expanded graphs, Phoenix trace annotations, and LangSmith trajectory observability.

Live traces: `duo` prints a loopback viewer URL (`/traces` shows it; `DUO_TRACE_PORT` pins it). The viewer (`viewer.html`, D3 vendored, works offline) is one page built to compare Codex and Claude:
1. Verdict: a plain-language headline, a head-to-head win bar, win rate with a 95% Wilson interval, a sign-test p-value (marked "inconclusive" under 20 decisive turns), judge agreement, and how often a verdict holds when the answer order is swapped.
2. Scoreboard: paired bars for judge score (±95% CI), runtime evals passed, tool success, median time, failed runs and blind retries. The better side is solid, the worse side faded.
3. Judge rubric: a dumbbell per dimension (correctness, completeness, groundedness, instruction following, efficiency) plus a self-preference check (does each judge favor its own model?).
4. Turn by turn: one row per task with the winner, a score Δ bar, a per-dimension Δ heatmap, a time dumbbell, tool counts/failures and failure tags. Sort by newest, biggest gap or most failures.
5. Execution timeline for long-running agents: an overview strip with a brush, scroll/drag zoom, idle gaps compressed, repeated tools collapsed into "Read×14" blocks that split as you zoom, ✗ failed calls, ◆ reasoning steps, and "Jump to" buttons that step through failed runs, failed calls, retry loops, slow (>p95) calls and long-running agents.
6. Tools and failures: paired per-tool bars (red = failed share), and a failure taxonomy (runtime failures + judge tags) whose rows filter a clickable case list.
Click anything to open a details drawer: the turn's judge verdicts and rationale, raw judge output, per-run tool sequence, each tool call's input/output, and the model's reasoning text. All of it loads on demand. Filters: task type (answer/read/write) and judge (panel, Codex-only, Claude-only). "Copy report" exports Markdown.

LLM/agent-as-judge (`judge.py`): after each turn, Duo grades the answers in the background. `/judge panel` (the chat default) has Codex and Claude each judge blind, with A/B labels and no model names, and in both orders. A win only counts when both orders agree, otherwise it is a tie. The rubric is fixed 1–5 with a closed failure-tag vocabulary, and trace metadata (tools, errors, latency) is given to the judge as evidence. `/judge claude|codex` uses a single judge. `/judge agent` uses read-only tool-enabled judges that check the workspace for work turns. `/judge off` disables it. One-shot: `duo --eval-judge panel "question"` prints the scores. Cost: panel = 4 extra calls per turn; `DUO_JUDGE_SWAP=0` halves it. `DUO_JUDGE_MODEL` overrides the judge model. Judge calls never count as contestant runs or evals.

Trace-aware judging: judges read the trajectory, not just the answer. Each candidate's ordered steps go to the judge under blind ids (A3, B7…): reasoning, every tool call with its target and ✓/✗, and failure output. Long runs are compressed: repeated successful calls are folded, the middle is trimmed, and every failure is kept. The judge returns:
- outcome scores (correctness, completeness, groundedness, instruction following, efficiency);
- process scores (planning, tool choice, error recovery, verification; null = no evidence, excluded from averages);
- up to 6 labelled steps (critical error / wasted / good move);
- the root cause: the first step where the run went wrong, from a closed taxonomy (ignored_constraint, ignored_tool_error, blind_retry_loop, no_verification, wrong_tool, bad_arguments, misread_context, hallucinated_result, premature_stop, scope_violation, slow_path, missing_context);
- up to 3 fixes, each with step evidence.
Step ids are validated per candidate and mapped back to the real event ids, so the viewer shows each annotation on the exact step. A verdict where only one swapped order parsed is kept but not counted as position-consistent.

Traces → actions → better agents (`lessons.py`): proposed fixes are grouped across turns and sessions, and near-duplicate rules filed under different keys are merged. `/improve` lists them and `/improve accept N [codex|claude|both]` adopts one. Lessons live in `~/.duo/lessons.json` and are prepended to that model's prompts; a `lessons.applied` event records which runs had lessons. `/lessons` lists them and `/lessons remove N` drops one. One-shot: `duo --improve`.

Self-learning, kept in check (`learning.py`):
- Feedback: your next message counts against or for the previous turn (a correction such as "no", "still broken" or re-asking the same thing, or a thank-you / "works"). `/rate good|bad [model]` and `/prefer codex|claude|tie` add explicit signals.
- Learned weights: Duo learns which dimensions matter to you, shrunk toward neutral until evidence accumulates (8 pseudo-signals). The rubric never changes, so scores stay comparable. Weights only re-rank fixes and highlight dimensions.
- Auto-lessons (chat default; `DUO_AUTO_LESSONS=0` disables): a fix is adopted automatically when it has ≥3 supporting turns from both judge families, up to 8 per model. Each lesson is measured before vs after adoption (score and root-cause rate) and retired automatically when it hurts (score −0.25, ≥5 turns). Auto-adoption pauses whenever the checks say judging is untrustworthy: judges disagree with your /prefer choices, verdicts flip when the order is swapped, or judge output fails to parse.
- Checks (`/learning`, and the viewer's Checks panel): sample size, task-mix balance, both judge families, order-swap consistency, judge parse rate, judge–user agreement, learned weights, lesson health, lesson cap, and suggestion diversity. Fixes are re-ranked so repeats of one failure type are down-weighted 40% each.
Viewer additions:
- "What to change": ranked fixes with evidence buttons that open the proving step.
- Checks panel, with "what matters to you" weights and lessons with before/after status.
- Process rows in the rubric, and root causes in the turn table and failure chart.
- ◎ root-cause markers and a jump button on the timeline.
- An annotated step-by-step trajectory in the drawer.

Fixed eval set + replay (`evalset.py`): compare models on the same tasks over time.
- `duo --evalset build [N]` picks up to N real past prompts from the trace DB. It skips write tasks (replaying them would modify files), short or feedback-only messages, anything that looks like a credential, and near-duplicates, and it alternates task kinds.
- `duo --evalset add "prompt"`, `list` and `remove ID` manage cases by hand.
- `duo --evalset import-history [N]` is opt-in: it pulls prompts from `~/.claude/history.jsonl`. Replays then send those prompts to Codex as well, so review them with `list` first.
- `duo --replay [N]` runs both models on every case with the panel judge. Read cases run in their original folder. Each run is saved to `~/.duo/replays/` with per-case scores, the winner, the judge setup, the lesson count and a fingerprint of the duo code.
- `duo --replay-compare [A B]` (default: the last two runs) shows per-model score deltas with a sign test, plus per-case winner flips.
- `duo --replay --no-lessons` followed by `duo --replay` is a controlled A/B test of whether lessons help.
- Expect ~6 model calls per case with the panel judge.

Judge calibration (`calibrate.py`):
- `duo --label [N]` shows past turns where both models answered, blind (A/B, random order). You pick a / b / tie / skip / quit. Labels are stored as feedback events in the original session.
- `duo --calibration` reports agreement with your labels for each judge and for the panel (95% Wilson interval), and which dimensions best predict your pick.
- Labels count like `/prefer`: they feed the learned weights and the "Judges agree with you" check, which pauses auto-learning when it fails.
- Lessons with no measurable effect after 15 turns are retired automatically.

Agents that get better next time (Oct 4). Design rule: nothing heavy on the hot path. Before a turn, duo only picks playbook lessons and a policy arm, which takes under 1 ms (0.7 ms measured with 200 lessons, budget 50 ms). Everything else runs after the answer is shown, in a background thread. `DUO_LIGHT=1` skips all post-turn model calls.
- Trace analysis (`analysis.py`, deterministic, milliseconds):
  - Each step is mapped to one of 9 action types.
  - Needleman-Wunsch aligns the two agents' runs and marks the first divergence. This only runs when both agents did the same task.
  - Behaviour flags: search loop, re-read churn, oscillation, write churn, acting on errors without diagnosing, finishing without verifying.
  - Exploration share and reasoning characters per tool call.
  - A reward-hack detector flags test-file edits, added skip/xfail, removed asserts, deleted tests, and grader or CI edits.
- Judge upgrades (same calls, no extra cost):
  - The judges receive the deterministic findings and are told to start at the divergence step.
  - They extract checkable claims, which duo then checks against the tool steps (supported / unsupported / contradicted).
  - Root cause is found counterfactually: earliest step → minimal fix → would it succeed?
  - Reasoning and actions are scored separately, and conflicts between them are listed.
  - A reward-hack verdict: a hack confirmed by both judges, or by one judge plus a deterministic signal, makes the other agent win the turn.
  - Cross-judge agreement on the root-cause step sets the confidence of step labels.
- Lessons become a playbook (`lessons.py`):
  - Itemized, with IDs, provenance and status (active / verified).
  - Only the top-k items relevant to the request are injected.
  - ExpeL-style votes from later outcomes: a lesson is harmful if its own failure type recurs.
  - The curator merges near-duplicates and retires harmful or no-effect items.
  - Confirmed failures on answer/read turns become regression cases in the eval set (capped per category).
- Statistics (`evalset.py`):
  - A stable 25% held-out split; `--replay --holdout`.
  - Paired bootstrap 95% CIs and a power hint in `--replay-compare`.
  - `duo --evolve` runs the held-out cases with lessons off, then on. Lessons are marked verified if the CI is above 0, and unverified ones are retired if it is below 0.
- RL on top of the agents (`policy.py`, black-box, no weights):
  - Reward is the judge score minus cheats and contradicted claims, adjusted by your feedback.
  - A Thompson-sampling bandit chooses how many lessons to inject (0/2/4) per agent and task type, with exploration capped at 10%.
  - Routing posterior per task type: P(Codex beats Claude). Applied only with `DUO_ROUTE=auto`.
  - `/policy` shows the arms.
- Agent PRs (`propose.py`):
  - `duo --propose [REPO]` writes a managed "Lessons from duo traces" section into AGENTS.md (Codex) and CLAUDE.md (Claude), with evidence: support, votes and the latest held-out A/B.
  - Proposals are versioned in `~/.duo/proposals` (a local git repo).
  - `duo --apply ID` writes the files. `duo --pr ID` creates a branch, commits, pushes and opens a draft PR via `gh`; it only runs on that explicit command.
  - `duo --proposals` lists them.
- Exports (`export.py`):
  - `duo --export-rl FILE [--format dpo|kto]` gives preference data. Chosen = your label, or a unanimous panel verdict, or a reward-hack override. Rows include trajectories and judge labels.
  - `duo --export-otel FILE` writes OTLP/JSON with OpenTelemetry GenAI v1.41 attributes (invoke_workflow / invoke_agent / execute_tool, evaluation events), for Phoenix, Langfuse or Docent.
- `duo --fork TURN [--samples N]`: counterfactual rerun of the faulty agent with the judges' minimal fix, scored again. It reports how often the outcome improves, which tests whether the diagnosed root cause was real. Answer/read turns only.
- Token and cost usage per run is captured from Claude stream-json / SDK results and Codex turn usage (best effort).
- Viewer additions, all linked by the selected turn:
  - Getting better: reward learning curve per agent with a 95% band, plus pins for lessons adopted, evolve runs and PRs.
  - RL policy: bandit arm intervals and the routing posterior.
  - Agent film strip: one glyph per step, shape = action type; Codex and Claude aligned column by column, with the divergence column, ✗ failures, ⚠ cheat signals and ◎ root causes marked; long matched stretches fold.
  - Phase flow (inspect → plan → edit → verify → run) per agent.
  - Said vs did: claims checked against tool evidence.
  - Behaviour fingerprint against healthy bands.
  - Agent PR board.
  - A cheat override on the turn table.

How the loop reaches the agents, without changing your intent:
- Before each agent run, duo picks the relevant playbook items, in under 1 ms. How many is decided by the RL bandit or by your setting.
- Your message is never edited. The items are APPENDED after the whole prompt under "Optional guidance… the user's request always wins; skip any item that does not apply".
- An item that conflicts with your request is dropped for that turn (e.g. "don't run tests" drops a "run the tests" lesson).
- The exact text each run received is stored and shown in that run's drawer ("Guidance this run received").
- Control it from the viewer's **Next prompt** panel: it previews exactly what each agent would get (optionally for a draft request), lists what is left out and why, and lets you Pin / Mute / Remove items, set items per turn (Auto via RL, 0, 2 or 4), or turn guidance off.
- The panel's controls only work from duo's own page: they need a custom header and a 127.0.0.1-only server.
- Standalone viewer over the trace DB: `python3 tools/serve_viewer.py [PORT]`.
- Streaming: the viewer follows the most recently active session ("Follow latest (live)"), tailing the trace DB, so it shows a duo chat running in another terminal within about a second. Panels refresh as events arrive. The **Live** strip shows each agent's steps as glyphs the moment they happen (the newest pops in, then pulses), a timer, the post-turn pipeline (agents → analysis → judges → learning → next prompt), and events per second.
- The viewer ends with a small collapsible "How duo works" file map. The full version is `FILE_MAP.html`; regenerate it with `python3 tools/file_map.py`.

Trace DB (`tracestore.py`): every event is persisted to SQLite at `~/.duo/traces.db` (`DUO_TRACE_DB=/path` moves it, `DUO_TRACE_DB=off` disables it). Tables: `sessions`, `spans`, `events`, `payloads` (reasoning, tool inputs/outputs, answers, prompts, raw judge output; ≤20k chars each) and `judge_scores` (judge × model × dimension). Past sessions open from the viewer's session picker. The live SSE stream stays metadata-only; payloads are fetched on demand via `/payload?id=` and `/span_payloads?span=`. Reasoning is captured from Codex reasoning summaries and Claude thinking blocks (Claude's respond mode now uses `stream-json`), when the model exposes them. Note: this means prompts, answers and reasoning are now stored on disk locally, unlike earlier Duo versions.
Example comparison: `sqlite3 ~/.duo/traces.db "select provider,dimension,round(avg(score),2) from judge_scores group by 1,2"`.

Tests: `python3 test_duo.py`.
Live checks: `python3 smoke_live.py` reads a harmless temporary file; `smoke_action.py` checks scratch implementation/review/synthesis. They consume provider usage.

Verified October 3, 2026: all 14 unit tests passed; both live file-read checks passed (Claude 5.14s, Codex 8.86s); scratch implementation, independent review, and synthesis passed in 27.6s. A scope restriction such as “do not modify any other file” now preserves the requested write action. These checks cover basic reads and writes, not every tool or approval path.

References: https://developers.openai.com/codex/app-server and https://code.claude.com/docs/en/agent-sdk/permissions
