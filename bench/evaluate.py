#!/usr/bin/env python3
"""Score Duo against duo-bench in one step: predict_duo.py (Duo's detectors -> predictions) + score.py.

    python3 bench/evaluate.py [--split dev|test|all] [--baseline bench/baselines/duo-1.0.0.json]
                              [--json out.json] [--md bench/REPORT.md] [--fail-on-regression] [--extra real.jsonl ...]

Adds Duo-only system checks (the viewer's JavaScript attribution must equal attribution.py). Exit status 1 when a
target is missed, or with --fail-on-regression when a metric is significantly worse than the baseline.
To score another system, write predictions in the same format and run bench/score.py on them.
"""
import argparse
import json
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import predict_duo, score  # noqa: E402

def run(split='all',baseline=None,extra=()):
    preds=predict_duo.predict_all(extra);checks,diffs=predict_duo.system_checks()
    notes=([''] + ['**Duo system checks:** '+'; '.join(diffs[:5])]) if diffs else []
    base=None
    if baseline:
        b=json.loads(Path(baseline).read_text());base={'metrics':{r['metric']:{'value':r['value']} for r in b['rows']}}
    return score.report(preds,split,'duo',base,extra,checks,notes)

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--split',default='all',choices=('dev','test','all'))
    ap.add_argument('--baseline');ap.add_argument('--json');ap.add_argument('--md');ap.add_argument('--extra',nargs='*',default=[])
    ap.add_argument('--fail-on-regression',action='store_true');ap.add_argument('--quiet',action='store_true')
    a=ap.parse_args(argv);out,md=run(a.split,a.baseline,a.extra)
    if a.json:Path(a.json).write_text(json.dumps(out,indent=1)+'\n')
    if a.md:Path(a.md).write_text(md)
    if not a.quiet:print(md)
    fail=any(r['status']=='FAIL' for r in out['rows']) or (a.fail_on_regression and any(r['regressed'] for r in out['rows']))
    if not a.quiet:print('RESULT:',('FAIL' if fail else 'PASS'),f"({sum(r['status']=='PASS' for r in out['rows'])} pass, {sum(r['status']=='FAIL' for r in out['rows'])} fail, {sum(r['status']=='KNOWN' for r in out['rows'])} known limits)")
    return 1 if fail else 0

if __name__=='__main__':sys.exit(main())
