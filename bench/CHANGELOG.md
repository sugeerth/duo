# agentlens-bench changelog

Versioning rules: bench/SPEC.md §7. Results are only comparable within one MAJOR version.

## 1.2.0 (2026-11-13)

- Renamed from duo-bench to agentlens-bench, with the project (Duo is now AgentLens). Case ids, labels and the prediction format are unchanged.
- New dataset `horizon.jsonl` (48 cases) and task T14, long-horizon failure modes (concepts from AgentDiff's long-horizon suite). There are 9 planted modes × 4 runs, plus 12 controls with look-alikes; runs are 76–151 steps.
- The reference system catches 36/36 at the labelled step, with 0/12 controls flagged and milestones 48/48. The caveat is in SPEC §4.
- While building T14, the first regression rule flagged every correct run: per-unit checks never re-run after a later unit's write. It now needs a late write to a file that an earlier passing check covered, and no re-run of that check or of the whole suite.
- The `swallowed_error` plant is now a failed edit that is never retried. It was a failing test file that the run's final whole-suite check covers, so it was not really swallowed.
  - Running horizon over the other families also found two false alarms, both fixed:
    - a failed check counted as swallowed even though a later whole-suite run covers it;
    - deleting build output (`rm -rf build/`) before a push counted as shipping an unchecked write.
  - Read-only diagnoses no longer count a failing check as swallowed.
- New system check `horizon.false_alarm_rate_clean_runs`: long-horizon modes on every clean family of the other tasks; target 0.
- System-check rates now count lower as better when comparing against a baseline.
- `bench/scale.py` streams the T14 generator at any size; results in `SCALE.md`.
- Earlier cases and labels are byte-identical (own random stream). Baseline `baselines/agentlens-1.2.0.json`.

## 1.1.0 (2026-11-12)

- New dataset `security.jsonl` (87 cases) and tasks T12 (security of tool calls) and T13 (tool-call error kinds).
  - Planted risky calls in every category, secrets in outputs, 10 obeyed and 10 ignored prompt injections, and 24 benign look-alike controls.
  - A trap family: injection strings inside a repo's own test fixtures, reported as a known limit.
  - 12 labelled tool failures.
- Existing 1.0.0 cases and labels are byte-identical; the new cases come from their own random stream.
- AgentLens system check `clusters.purity` (patterns-map clusters vs planted run families; target ≥ 0.6).
- Building T12 found a real detector miss: `curl -d @.env` was not flagged as secret exposure. The case was added, confirmed failing, then fixed.
- Baseline `baselines/duo-1.1.0.json`.

## 1.0.0 (2026-11-12)

First formal release.
- 256 cases: 120 runs in 12 families, 80 trend series, 40 attribution answers, 10 RL tasks (78 episodes) and 6 import fixtures; dev/test split by id hash; SHA-256 checksums in the manifest.
- 11 tasks (T1–T11) with a public prediction format (`schema/prediction.schema.json`), so any system can be scored with `score.py`.
- Wilson intervals for proportions and a family-stratified bootstrap for F1; targets; known limits; baseline comparison with regression detection.
- Reference system: AgentLens (`predict_agentlens.py`); baseline `baselines/duo-1.0.0.json`, report `REPORT.md`.

Before 1.0.0 (unreleased drafts): the benchmark found and fixed two AgentLens detector bugs (`2>&1` read as a file write; attribution threshold 0.25 → 0.5, chosen on dev) and several generator mistakes (untruthful labels; trend drifts that saturated at the clamps).
