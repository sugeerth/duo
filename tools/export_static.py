#!/usr/bin/env python3
"""Export the viewer and a trace DB as ONE self-contained HTML file: shareable, no server, no live stream.

    python3 tools/export_static.py out.html [--db PATH] [--current SESSION] [--max-payload 4000]

Every route the viewer reads (/sessions, /snapshot, /payload, /span_payloads, /fleet, /facets, /longterm, ...) is
answered from data embedded in the page; the live stream is replaced by the stored events. Controls that write
(lessons, policy) are read-only here. Payloads (tool outputs, reasoning, answers) are included, cut to --max-payload
characters each, so review what the DB holds before sharing the file.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
D3_CDN='https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js'

SHIM=r'''<script>
// Static export: answer the viewer's routes from embedded data; no live stream.
(function(){const D=JSON.parse(document.getElementById('agentlens-data').textContent);
const ok=o=>Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
const nf=()=>Promise.resolve(new Response(JSON.stringify({error:'not in this export'}),{status:404,headers:{'Content-Type':'application/json'}}));
const real=window.fetch.bind(window);
window.fetch=function(u,opt){const s=String(u);if(!s.startsWith('/'))return real(u,opt);
  const url=new URL(s,'http://x'),p=url.pathname,q=Object.fromEntries(url.searchParams),sess=q.session||D.current;
  if(opt&&opt.method&&opt.method!=='GET')return Promise.resolve(new Response(JSON.stringify({error:'read-only export'}),{status:403}));
  if(p==='/sessions')return ok({current:D.current,db:null,sessions:D.sessions});
  if(p==='/snapshot')return D.snapshots[sess]?ok(D.snapshots[sess]):nf();
  if(p==='/payload'){const x=(D.payloads[sess]||{})[q.id];return x?ok(x):nf()}
  if(p==='/span_payloads'){const ev=(D.snapshots[sess]||{}).events||[],P=D.payloads[sess]||{};
    return ok(ev.filter(e=>e.span_id===q.span&&P[e.id]).map(e=>({event_id:e.id,kind:P[e.id].kind,provider:e.provider,text:P[e.id].text})))}
  if(p in D.routes)return ok(D.routes[p]);return nf()};
window.EventSource=function(){this.close=()=>{};setTimeout(()=>{try{this.onopen&&this.onopen()}catch(e){}},0)};
})();
</script>'''

def build(db,out,current=None,max_payload=4000):
    os.environ['AGENTLENS_TRACE_DB']=str(db)
    import tracestore,fleet,facets,longterm
    st=tracestore.TraceStore(str(db));st.flush(5)
    sess=[s for s in st.sessions(limit=500) if s.get('turns')];ids=[s.get('id') or s.get('session_id') for s in sess]  # skip empty sessions
    snaps={};pays={}
    for sid in ids:
        snap=st.snapshot(sid)
        if not snap:continue
        snaps[sid]=snap;P={}
        for e in snap['events']:
            if e.get('chars'):
                x=st.payload(sid,e['id'])
                if x:x=dict(x);x['text']=str(x.get('text') or '')[:max_payload];P[str(e['id'])]=x
        pays[sid]=P
    routes={}
    for path,fn in (('/fleet',lambda:fleet.snapshot(st)),('/facets',lambda:facets.snapshot(st)),('/longterm',lambda:longterm.snapshot(st))):
        try:routes[path]=fn()
        except Exception as exc:routes[path]={'error':str(exc)}
    try:
        import learning;routes['/learning']=learning.snapshot(st)
    except Exception:pass
    try:
        import agentlens;routes['/filemap']=agentlens.file_map_data()
    except Exception:pass
    cur=current if current in snaps else (ids[0] if ids else None)
    data={'current':cur,'sessions':sess,'snapshots':snaps,'payloads':pays,'routes':routes}
    html=(ROOT/'viewer.html').read_text()
    blob=json.dumps(data,ensure_ascii=False,default=str).replace('</','<\\/')
    head=f'<script type="application/json" id="agentlens-data">{blob}</script>\n{SHIM}\n'
    assert '<script src="/d3.v7.min.js"></script>' in html
    html=html.replace('<script src="/d3.v7.min.js"></script>',head+f'<script src="{D3_CDN}"></script>',1)
    Path(out).write_text(html)
    return {'sessions':len(snaps),'events':sum(len(s['events']) for s in snaps.values()),'payloads':sum(len(p) for p in pays.values()),'bytes':len(html.encode())}

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__.split('\n')[0]);ap.add_argument('out');ap.add_argument('--db');ap.add_argument('--current');ap.add_argument('--max-payload',type=int,default=4000)
    a=ap.parse_args(argv)
    import tracestore
    db=a.db or os.environ.get('AGENTLENS_TRACE_DB') or str(Path.home()/'.agentlens'/'traces.db')
    print(json.dumps(build(db,a.out,a.current,a.max_payload)))
    return 0

if __name__=='__main__':sys.exit(main())
