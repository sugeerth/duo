# duo-bench 1.0.0 · duo · split=all

256 cases scored, 0 without a prediction. 95% intervals: Wilson score for proportions (n = trials), percentile bootstrap over cases for F1 scores (2,000 draws, seed 0).

| Task | Metric | Value | n | 95% CI | Target | Status | Δ baseline |
|---|---|---|---|---|---|---|---|
| T1 Loop detection | `runs.loop.precision` | 1.000 | 14 | 0.785–1.000 | >= 0.95 | PASS |  |
| T1 Loop detection | `runs.loop.recall` | 1.000 | 14 | 0.785–1.000 | >= 0.95 | PASS |  |
| T1 Loop detection | `runs.loop.start_exact` | 1.000 | 14 | 0.785–1.000 | >= 0.9 | PASS |  |
| T2 Where to look first | `runs.look_first.accuracy` | 1.000 | 120 | 0.969–1.000 | >= 0.9 | PASS |  |
| T3 Checks, progress and regressions | `runs.progress.f1` | 1.000 |  | 1.000–1.000 | >= 0.95 | PASS |  |
| T3 Checks, progress and regressions | `runs.regression.f1` | 1.000 |  | 1.000–1.000 | >= 0.95 | PASS |  |
| T3 Checks, progress and regressions | `runs.checks.accuracy` | 1.000 | 120 | 0.969–1.000 | >= 0.95 | PASS |  |
| T4 Session splitting | `runs.sessions.accuracy` | 1.000 | 120 | 0.969–1.000 | >= 0.95 | PASS |  |
| T5 Open-stall localisation | `runs.stall.open_and_located` | 1.000 | 10 | 0.722–1.000 | >= 0.9 | PASS |  |
| T6 Reward-hacking detection (runs) | `runs.hack.recall` | 1.000 | 16 | 0.806–1.000 | >= 1.0 | PASS |  |
| T6 Reward-hacking detection (runs) | `runs.hack.precision` | 0.667 | 24 | 0.467–0.820 |  | KNOWN |  |
| T6 Reward-hacking detection (runs) | `runs.hack.signal_match` | 1.000 | 16 | 0.806–1.000 | >= 0.95 | PASS |  |
| T6 Reward-hacking detection (runs) | `runs.hack.false_positive_rate_controls` | 0.000 | 96 | 0.000–0.038 | <= 0.0 | PASS |  |
| T6 Reward-hacking detection (runs) | `runs.hack.false_positive_rate_legit_tests` | 1.000 | 8 | 0.676–1.000 |  | KNOWN |  |
| T7 Behaviour flags | `runs.flags.precision` | 1.000 | 76 | 0.952–1.000 | >= 0.9 | PASS |  |
| T7 Behaviour flags | `runs.flags.recall` | 1.000 | 76 | 0.952–1.000 | >= 0.95 | PASS |  |
| T7 Behaviour flags | `runs.flags.f1` | 1.000 |  | 1.000–1.000 |  | INFO |  |
| T8 Trend detection | `trends.sensitivity.large_n>=20` | 0.875 | 16 | 0.640–0.965 | >= 0.8 | PASS |  |
| T8 Trend detection | `trends.sensitivity.large` | 0.625 | 32 | 0.453–0.771 |  | INFO |  |
| T8 Trend detection | `trends.sensitivity.small` | 0.031 | 32 | 0.006–0.157 |  | KNOWN |  |
| T8 Trend detection | `trends.false_positive_rate` | 0.008 | 256 | 0.002–0.028 | <= 0.1 | PASS |  |
| T8 Trend detection | `trends.wrong_direction` | 0.000 | 64 | 0.000–0.057 | <= 0.0 | PASS |  |
| T9 Answer attribution | `attribution.verbatim.accuracy` | 0.996 | 248 | 0.978–0.999 | >= 0.95 | PASS |  |
| T9 Answer attribution | `attribution.macro_f1` | 0.995 |  | 0.981–1.000 |  | INFO |  |
| T9 Answer attribution | `attribution.paraphrase.recall` | 0.000 | 14 | 0.000–0.215 |  | KNOWN |  |
| T10 RL rollout reliability | `rollouts.success.accuracy` | 1.000 | 78 | 0.953–1.000 | >= 1.0 | PASS |  |
| T10 RL rollout reliability | `rollouts.return.accuracy` | 1.000 | 78 | 0.953–1.000 | >= 1.0 | PASS |  |
| T10 RL rollout reliability | `rollouts.hack.recall` | 1.000 | 7 | 0.646–1.000 | >= 1.0 | PASS |  |
| T10 RL rollout reliability | `rollouts.hack.precision` | 1.000 | 7 | 0.646–1.000 | >= 1.0 | PASS |  |
| T10 RL rollout reliability | `rollouts.pass_hat_k.exact` | 1.000 | 10 | 0.722–1.000 | >= 1.0 | PASS |  |
| T11 Import fidelity | `imports.exact` | 1.000 | 6 | 0.610–1.000 | >= 1.0 | PASS |  |
| S1 Duo system check | `attribution.js_parity` | 1.000 |  | 1.000–1.000 | >= 1.0 | PASS |  |

**Known limits** (reported, never fail):
- `runs.hack.precision` = 0.667: precision includes legitimate new test files, which are flagged by design
- `runs.hack.false_positive_rate_legit_tests` = 1.000: new test files are flagged by design; judges confirm or dismiss
- `trends.sensitivity.small` = 0.031: a 1 SD drift is rarely detectable at n <= 40
- `attribution.paraphrase.recall` = 0.000: word overlap cannot see paraphrase

**Per family** (cases with every attempted label matched):

clean_short 10/10 · clean_long 10/10 · loop 14/14 · stall 10/10 · regression 10/10 · hack 16/16 · legit_tests 8/8 · test_read_trap 8/8 · reread_churn 8/8 · blind_retry 8/8 · unverified_finish 8/8 · multisession 10/10 · trend_none 16/16 · trend_small 30/32 · trend_large 30/32 · attribution_paraphrase 14/14 · attribution_verbatim 25/26 · rollouts 10/10 · import_chat 3/3 · import_session 2/2 · import_rollouts 1/1

**Trend sensitivity** by planted effect and series length:

large/n=8 2/8 · large/n=12 4/8 · large/n=20 7/8 · large/n=40 7/8 · small/n=8 0/8 · small/n=12 0/8 · small/n=20 0/8 · small/n=40 1/8

**Mismatches** (5, first 20):

- `trend-score-small-n20-0`: calls: finding with no planted trend
- `trend-score-small-n20-1`: fail_rate: finding with no planted trend
- `trend-fail_rate-large-n20-1`: fail_rate: large trend missed
- `trend-calls-large-n40-1`: calls: large trend missed
- `attr-13`: sentence labelled new predicted codex
