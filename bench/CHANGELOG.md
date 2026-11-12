# duo-bench changelog

Versioning rules: bench/SPEC.md §7. Results are only comparable within one MAJOR version.

## 1.1.0 (2026-11-12)

- New dataset `security.jsonl` (87 cases) and tasks T12 (security of tool calls) and T13 (tool-call error kinds).
  - Planted risky calls in every category, secrets in outputs, 10 obeyed and 10 ignored prompt injections, and 24 benign look-alike controls.
  - A trap family: injection strings inside a repo's own test fixtures, reported as a known limit.
  - 12 labelled tool failures.
- Existing 1.0.0 cases and labels are byte-identical; the new cases come from their own random stream.
- Duo system check `clusters.purity` (patterns-map clusters vs planted run families; target ≥ 0.6).
- Building T12 found a real detector miss: `curl -d @.env` was not flagged as secret exposure. The case was added, confirmed failing, then fixed.
- Baseline `baselines/duo-1.1.0.json`.

## 1.0.0 (2026-11-12)

First formal release.
- 256 cases: 120 runs in 12 families, 80 trend series, 40 attribution answers, 10 RL tasks (78 episodes) and 6 import fixtures; dev/test split by id hash; SHA-256 checksums in the manifest.
- 11 tasks (T1–T11) with a public prediction format (`schema/prediction.schema.json`), so any system can be scored with `score.py`.
- Wilson intervals for proportions and a family-stratified bootstrap for F1; targets; known limits; baseline comparison with regression detection.
- Reference system: Duo (`predict_duo.py`); baseline `baselines/duo-1.0.0.json`, report `REPORT.md`.

Before 1.0.0 (unreleased drafts): the benchmark found and fixed two Duo detector bugs (`2>&1` read as a file write; attribution threshold 0.25 → 0.5, chosen on dev) and several generator mistakes (untruthful labels; trend drifts that saturated at the clamps).
