"""Standalone viewer over the trace DB (no chat session needed): python3 tools/serve_viewer.py [PORT]"""
import os,sys,time
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agentlens,traces,tracestore,learning,policy,propose,longterm,facets
traces.HUB.attach_store(tracestore.open_default())
traces.HUB.routes['/learning']=lambda q=None:learning.snapshot(traces.HUB.store)
traces.HUB.routes['/policy']=lambda q=None:policy.summary()
traces.HUB.routes['/longterm']=lambda q=None:longterm.snapshot(traces.HUB.store)
traces.HUB.routes['/facets']=lambda q=None:facets.snapshot(traces.HUB.store)
traces.HUB.routes['/proposals']=lambda q=None:[propose.get(p['id']) or p for p in propose.load_index()]
traces.HUB.routes['/next']=agentlens.next_preview
traces.HUB.routes['/filemap']=lambda q=None:agentlens.file_map_data()
traces.HUB.post_routes['/lessons']=agentlens.lessons_update
port=traces.HUB.start(int(sys.argv[1]) if len(sys.argv)>1 else 8790)
print(f'Viewer: http://127.0.0.1:{port}  (pick a session top-right; Ctrl+C to stop)',flush=True)
while True:time.sleep(3600)
