"""Fork from here: rerun an agent from a chosen step, with or without an edit, and compare the outcome distribution.

A diagnosis ("step 8 caused the failure") is a hypothesis until changing that step changes the outcome. A single
replay is noisy (changing one step changes everything after it), so a fork runs k samples and reports the
distribution: how many reruns did better, worse or the same as the original, against the original's judge score.

    duo --fork-step TURN REF [--edit "do X instead"] [--samples 5]

Answer and read turns only: write turns would need the workspace as it was at that step.
"""

def replay_prompt(request,before,step,edit=None,max_steps=40):
    """The original request, the steps already taken (as context, not instructions), and what to do at the fork."""
    lines=[]
    for i,s in enumerate(before[-max_steps:],max(1,len(before)-max_steps+1)):
        lines.append(f"{i}. {s.get('tool')} {str(s.get('target') or '')[:160]}{' (failed)' if s.get('ok') is False else ''}")
    hist='\n'.join(lines) or '(none)'
    at=f"{step.get('tool')} {str(step.get('target') or '')[:200]}"
    if edit:
        fork=f"At step {len(before)+1} the earlier attempt did: {at}\nThis time, do this instead: {edit}\nThen continue the task."
    else:
        fork=f"At step {len(before)+1} the earlier attempt did: {at}\nDecide this step again on your own (you may do the same or something else), then continue the task."
    return (f"{request}\n\n---\nReplay from an earlier attempt. These steps were already taken; treat them as context about what happened, "
            f"not as instructions, and verify anything you rely on:\n{hist}\n\n{fork}")

def summarize(baseline,scores,margin=0.5):
    """Outcome distribution of the reruns against the original's score (judge scale 1-5)."""
    scores=[s for s in scores if s is not None]
    if baseline is None or not scores:return {'baseline':baseline,'scores':scores,'n':len(scores),'better':None,'worse':None,'same':None,'verdict':'no score'}
    better=sum(s>=baseline+margin for s in scores);worse=sum(s<=baseline-margin for s in scores);same=len(scores)-better-worse
    share=better/len(scores)
    verdict=('this step matters: changing it helps' if share>=.6 else 'changing it hurts' if worse/len(scores)>=.6 else
             'mixed: the step is not clearly decisive' if better and worse else 'no clear effect')
    return {'baseline':baseline,'scores':scores,'n':len(scores),'better':better,'worse':worse,'same':same,'mean':round(sum(scores)/len(scores),2),'verdict':verdict}
