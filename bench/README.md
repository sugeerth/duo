# Duo benchmark: labelled data for every detector

Synthetic, organised, labelled cases to test Duo's analysis against. Each label comes from how the case was
**planted** by `generate.py`, never from running a detector. So `evaluate.py` measures the detectors; it does not
grade them against their own output.

```sh
python3 bench/generate.py                    # rewrite bench/data (deterministic: seed 7, byte-identical)
python3 bench/evaluate.py                    # score every detector; exit 1 if any target is missed
python3 bench/evaluate.py --split test       # held-out split only
python3 bench/evaluate.py --json out.json    # full metrics, per-family results and every mismatch
python3 bench/export_for_labeling.py --out bench/data/real/todo.jsonl   # real turns, ready to label
python3 bench/evaluate.py --extra bench/data/real/*.jsonl                # include your reviewed real labels
```

## What is in it

256 cases, about 1 MB. A stable hash of each case id assigns about 30% to `test` and the rest to `dev`; import fixtures are all `test`.

| Dataset | File | Cases | What is labelled | Detector under test |
|---|---|---|---|---|
| Runs | `data/runs.jsonl` | 120, in 12 families | loops, look-first step, progress and regression checks, sessions, open stalls, reward-hacking steps and signals, behaviour flags, check count | `longrun.read`, `analysis.hack_signals`, `analysis.behaviour` |
| Trends | `data/trends.jsonl` | 80 | which metric carries a planted trend and its direction, or none | `longterm.insight` |
| Attribution | `data/attribution.jsonl` | 40 | true source of every sentence: codex, claude, both, new; paraphrase and light-edit flags | `attribution.attribute` and the viewer's JS |
| Rollouts | `data/rollouts.json` + `data/rollouts/*.json` | 10 tasks, 78 episodes | per-episode success and return, hacked episodes, exact pass@k and pass^k | `chatimport.import_rollouts` + hack detector |
| Imports | `data/imports.json` + `data/imports/*` | 6 fixtures | exact turn, tool, failure, reasoning, answer and context-point counts | `chatimport` (chat, session, rollout loaders) |

`data/manifest.json` lists every case with its dataset, family, split, file, size and a one-line note, plus counts per family.

### Run families

Each run is a list of tool steps, half in Claude Code style (`Read`, `Edit`, `Bash`) and half in Codex style (`commandExecution`, `fileChange`).

| Family | n | What is planted | Why it is there |
|---|---|---|---|
| `clean_short` | 10 | read, edit, one passing check | baseline |
| `clean_long` | 10 | an hour of bursts separated by idle, new files each burst, steady progress | **control**: long but healthy. No loop or stall may be reported |
| `loop` | 14 | a failing check, then 3–6 bursts repeating the same read, edit and check, with idle in between (some gaps over 30 min), then a fix | loop detection and its exact start |
| `stall` | 10 | one progress event, then 15–30 min of non-repeating work with failing checks | open-stall detection without a loop |
| `regression` | 10 | a passing check fails after an unrelated edit, then is fixed | regression detection |
| `hack` | 16 | one of: asserts removed from a test, a skip added, a grader or CI file edited, a test file deleted | reward-hacking recall and signal accuracy |
| `legit_tests` | 8 | a NEW test file with real asserts | **trap**: not a hack. The detector flags it by design (the judges confirm), so this is a reported known limit |
| `test_read_trap` | 8 | test files read and printed, but no check ever runs | **trap**: reading a test file is not a check |
| `reread_churn` | 8 | one file re-read 4+ times within 10 steps | behaviour flag |
| `blind_retry` | 8 | a failure followed by three actions with no diagnosis | behaviour flag |
| `unverified_finish` | 8 | edits after the last check | behaviour flag |
| `multisession` | 10 | 2–4 sessions separated by idle of 30 min to 4 h | session splitting |

## Label definitions (runs)

All `*_ref` values are step `ref`s in the same record.

- **check**: a command that runs tests, lint or type checks. `pytest -q` and `cd repo && pytest -q 2>&1 | tail -5` are the *same* check; `python -m pytest tests/ -q` is a different one. Reading, printing or editing a test file is not a check.
- **progress_refs**: checks that pass where the same check's previous run did not (its first pass counts), or that pass more cases than it ever had. The generator records these as it plants each check.
- **regression_refs**: checks that fail where the same check last passed.
- **loops**: `start_ref` is the first step of the first burst that *repeats* an earlier burst. The original failing burst is not part of the loop. `bursts` is the number of repeats.
- **look_first_ref**: the start of the longest loop; else the start of an open stall with at least 10 minutes of work; else the first regression; else null.
- **stall_open / stall_start_ref**: the run ends without progress after `stall_start_ref`, which is the last progress event, or the first step if there was none.
- **sessions**: split by idle of at least 30 minutes.
- **hacks**: `{ref, signals}`, where signals come from `test_file_edit`, `assert_removed`, `skip_added`, `grader_or_ci_edit` and `test_deleted`.
- **flags**: every behaviour flag that is *true* of the run (`reread_churn`, `write_churn`, `rogue_after_error`, `unverified_finish`, `search_loop`, `oscillation`), including incidental ones. A loop really does re-read and re-edit.

## Other datasets

- **Trends**: one model, n of 8, 12, 20 or 40 turns. Exactly one metric (seconds, score, fail_rate or calls) drifts by a total of 1 SD (small) or 3 SD (large) of its own noise across the series, up or down. The other metrics are pure noise and serve as controls in every case. There are 16 cases with no drift at all. A finding on an undrifted metric is a false positive.
- **Attribution**: sentences come from 8 frames and 24–32-word vocabularies. A combined answer mixes:
  - verbatim sentences from each candidate;
  - sentences both candidates contain (`both`);
  - genuinely new sentences;
  - light edits (one word changed; the true source still counts);
  - paraphrases (synonyms and reordering; the true source is the gold label even though word overlap cannot see it).
- **Rollouts**: success probability from 0 to 1 and 4–12 episodes per task. Hacked episodes edit the test file and are reported successful by the environment. `pass_at_k` and `pass_hat_k` are exact, from the gold successes.
- **Imports**: messy real-shaped files:
  - a claude.ai export with an empty conversation;
  - a ChatGPT export with an abandoned regenerated branch and a system message;
  - a Claude Code log with a `/clear` wrapper, sidechain records and `is_error` results;
  - a Codex CLI rollout with an `environment_context` wrapper and exit codes;
  - rollouts in step-record form.

## Results (all splits)

`bench/results.json` holds the full output of the last run. Summary:

| Detector | Result |
|---|---|
| Loops | precision 1.00, recall 1.00, exact start 1.00 |
| Look-first step | 1.00 |
| Progress / regressions | F1 1.00 / 1.00 |
| Sessions, check counting (including traps) | 1.00, 1.00 |
| Reward hacking (runs) | recall 1.00, signals 1.00, false positives on controls 0 |
| Behaviour flags | precision 1.00, recall 1.00 |
| Trends | sensitivity 0.88 for 3 SD drifts with n ≥ 20; false-positive rate 0.008; wrong direction 0 |
| Attribution | verbatim and light-edit accuracy 0.996; JS ↔ Python parity 1.00 |
| Rollouts | success, return, hack flags and pass^k all exact |
| Imports | 6/6 exact |

**Known limits**, reported rather than hidden:

- The hack detector flags 8/8 legitimate new test files. This is deterministic by design; the judges are meant to confirm or dismiss.
- Attribution recognizes 0 of 14 paraphrases, because word overlap cannot see a paraphrase.
- A 1 SD drift is almost never detected at n ≤ 40 (1/32). Trend sensitivity rises with n and effect: a 3 SD drift is found 2/8 times at n=8, 4/8 at n=12, and 7/8 at n=20 and n=40.

## What building it fixed

Running the detectors against planted labels found real bugs, now fixed:

1. `analysis.action_type` read any `>` in a command as a file write, so `pytest -q 2>&1 | tail -5` was classified as an edit. That check was never counted (progress and regressions were missed), and finished runs were flagged `unverified_finish`. The viewer's JS had the same bug.
2. Attribution's 0.25 threshold let shared stock phrases count as a source. Raised to 0.5, chosen on the **dev** split (0.995 on dev) and confirmed on test, in both Python and the viewer.
3. Editing or reading a test file counted as a check (fixed earlier; the `test_read_trap` family now guards it).

## Limits of this data

- It is synthetic, and the same author wrote the generator and the detectors, so a shared misconception could pass unnoticed. The traps, the controls and the held-out split reduce that risk but do not remove it. Real labelled runs are the remedy: `export_for_labeling.py` writes real turns with the detectors' output under `suggested`. A person fills `labels` after checking the steps and sets `"reviewed": true`; `evaluate.py --extra` scores only reviewed lines.
- Timings, file names and outputs are invented. Prompts, model text and tool outputs are placeholders. No real user data is included.
- The trend and attribution datasets test the statistics and the matching rule, not judge quality. Judge accuracy needs human labels (`duo --label`, `duo --calibration`).

## Schema (one run record)

```json
{"id": "run-loop-03", "family": "loop", "split": "dev", "style": "codex",
 "steps": [{"ref": 1, "tool": "commandExecution", "target": "sed -n 1,200p src/cache_4.py", "ok": true,
            "at": 1767041234.512, "seconds": 0.73, "output": "", "input": "..."}],
 "labels": {"loops": [{"start_ref": 9, "end_ref": 17, "bursts": 3}], "look_first_ref": 9,
            "progress_refs": [21], "regression_refs": [], "sessions": 2, "stall_open": null, "stall_start_ref": null,
            "hacks": [], "flags": ["reread_churn", "write_churn"], "checks": 5, "note": "..."}}
```
