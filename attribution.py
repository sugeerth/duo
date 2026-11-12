"""Answer attribution, the reference implementation of the viewer's `attribute()` (viewer.html).

Each sentence of a combined answer is matched against the candidate answers by word-trigram containment
(share of the sentence's trigrams found in the candidate). >= 0.5 counts as a source; when two sources both clear
it and the second is within 60% of the first, the sentence is "both"; when none clears it, "new". bench/evaluate.py
checks that this and the viewer's JavaScript give the same labels.
"""
import re

THRESHOLD=0.5   # chosen on the bench dev split (bench/README.md); 0.25 let shared stock phrases count as a source
BOTH_RATIO=0.6

def shingles(text):
    w=re.findall(r'[a-z0-9_]+',str(text).lower())
    out={' '.join(w[i:i+3]) for i in range(len(w)-2)}
    if w and len(w)<3:out.add(' '.join(w))
    return out

def sentences(text):
    parts=re.split(r'(?<=[.!?])\s+|\n+',str(text).replace('\r',''))
    return [p.strip() for p in parts if len(p.strip())>2]

def attribute(final,candidates):
    """[{text, src, scores}] with src in candidates' keys, 'both' or 'new'."""
    C={p:shingles(t) for p,t in candidates.items()}
    out=[]
    for text in sentences(final):
        S=shingles(text);sc={p:(len(S&C[p])/len(S) if S else 0.0) for p in C}
        ps=sorted(sc,key=lambda p:-sc[p]);src='new'
        if ps and sc[ps[0]]>=THRESHOLD:
            src='both' if len(ps)>1 and sc[ps[1]]>=THRESHOLD and sc[ps[1]]>=BOTH_RATIO*sc[ps[0]] else ps[0]
        out.append({'text':text,'src':src,'scores':sc})
    return out
