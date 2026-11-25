#!/usr/bin/env python3
"""Long horizons at scale: generate a procedural corpus of long runs, stream it through horizon.py, and report.

    python3 bench/scale.py --per-mode 100 --controls 300           # ~1,200 runs, ~140k steps
    python3 bench/scale.py --per-mode 1000 --controls 3000 --md bench/SCALE.md   # ~12,000 runs, ~1.4M steps

The corpus is the T14 generator (bench/generate.py iter_horizon) run with more cases per mode: every planted mode
in every run, plus controls carrying the look-alikes. Nothing is held in memory past one run: the generator yields
a run, the detectors read it, its counts are added, and it is dropped. The corpus is not shipped; the seed
regenerates it.

Besides the catch, it reports what an OUTCOME-ONLY grade would have said about each run: pass when the run's last
check passed and the answer claims the work is done. That is the AgentDiff headline at scale: how many failing runs
pass an outcome grade and are caught only by the long-horizon reading. Two caveats are printed with every result:
the generator and detectors were written together, so this is a floor on these nine modes, not a claim about modes
the corpus does not contain; and the outcome grade here is a rule, not a model judge.
"""
import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parent));sys.path.insert(0,str(HERE))
import generate  # noqa: E402
import horizon  # noqa: E402
from analysis import TEST_CMD  # noqa: E402

def outcome_pass(c):
    checks=[s for s in c['steps'] if s['tool']=='Bash' and TEST_CMD.search(s['target'])]
    return bool(checks) and checks[-1]['ok'] and bool(horizon.CLAIM_DONE.search(c.get('answer') or ''))

def run(per_mode,controls,seed=29):
    t0=time.perf_counter();gen=0.0;read=0.0;steps=0;n=0
    catch=Counter();total=Counter();located=0;known=0;extra=0;ctl=0;ctl_flag=0;ms_ok=0;graded_pass=0;graded_pass_caught=0;longest=0
    it=generate.iter_horizon(random.Random(generate.SEED*1000+seed),per_mode,controls)
    while True:
        a=time.perf_counter()
        try:c=next(it)
        except StopIteration:break
        b=time.perf_counter();r=horizon.read(c['steps'],c['request'],c['answer'],c['stop_reason']);d=time.perf_counter()
        gen+=b-a;read+=d-b;n+=1;steps+=len(c['steps']);longest=max(longest,len(c['steps']))
        got={(m['mode'],m['ref']) for m in r['modes']};gm={m for m,_ in got};want=c['labels']['modes']
        ms_ok+=r['milestones']['reached']==c['labels']['milestones_reached']
        if want:
            w=want[0];known+=1;total[w['mode']]+=1;hit=w['mode'] in gm;catch[w['mode']]+=hit;located+=(w['mode'],w['ref']) in got;extra+=len(gm-{w['mode']})
            if outcome_pass(c):graded_pass+=1;graded_pass_caught+=hit
        else:ctl+=1;ctl_flag+=bool(gm)
    wall=time.perf_counter()-t0
    return {'runs':n,'steps':steps,'longest_run':longest,'known_failures':known,'caught':sum(catch.values()),'located':located,'extra_modes':extra,
            'per_mode':{m:f'{catch[m]}/{total[m]}' for m in sorted(total)},'controls':ctl,'controls_flagged':ctl_flag,'milestones_exact':ms_ok,
            'graded_pass_by_outcome':graded_pass,'graded_pass_caught':graded_pass_caught,
            'seconds':{'generate':round(gen,2),'read':round(read,2),'wall':round(wall,2)},'steps_per_second_read':int(steps/read) if read else None,
            'params':{'per_mode':per_mode,'controls':controls,'seed':seed}}

def report(r):
    return '\n'.join([
        f"# Long horizons at scale · {r['runs']:,} runs · {r['steps']:,} steps",'',
        f"Generated and read in {r['seconds']['wall']:.0f} s ({r['seconds']['read']:.0f} s reading: {r['steps_per_second_read']:,} steps/s, one run in memory at a time; longest run {r['longest_run']} steps).",'',
        f"> **{r['caught']:,} of {r['known_failures']:,} known failures caught**, {r['located']:,} at the labelled step; "
        f"**{r['graded_pass_by_outcome']:,} were graded a pass by the outcome**, {r['graded_pass_caught']:,} of them caught by the long-horizon reading; "
        f"**{r['controls_flagged']:,} of {r['controls']:,} control runs flagged**; {r['extra_modes']:,} extra modes on planted runs; milestones exact on {r['milestones_exact']:,} of {r['runs']:,}.",'',
        '| mode | caught |','|---|---|']+[f'| `{m}` | {v} |' for m,v in r['per_mode'].items()]+['',
        f"Reproduce: `python3 bench/scale.py --per-mode {r['params']['per_mode']} --controls {r['params']['controls']}` (seed {r['params']['seed']}; the corpus is regenerated, not shipped).",'',
        '**Caveats.** The generator and the detectors were written together, so this is a floor on these nine modes, not a claim about modes the corpus does not contain; '
        'the per-mode counts show it is not one mode carrying the total. The outcome grade is a rule (last check passed and the answer claims done), not a model judge. '
        'Values-based modes (stale value, drift, late fault) are not covered.',''])

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('--per-mode',type=int,default=100);ap.add_argument('--controls',type=int,default=300)
    ap.add_argument('--seed',type=int,default=29);ap.add_argument('--json');ap.add_argument('--md')
    a=ap.parse_args(argv);r=run(a.per_mode,a.controls,a.seed);md=report(r)
    if a.json:Path(a.json).write_text(json.dumps(r,indent=1)+'\n')
    if a.md:Path(a.md).write_text(md)
    print(md)
    return 0 if r['caught']==r['known_failures'] and r['controls_flagged']==0 else 1

if __name__=='__main__':sys.exit(main())
