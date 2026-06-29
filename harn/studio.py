"""harn studio — a local visual editor for the workflow + skills (no deps).

`harn ui` serves this on http://127.0.0.1:9999. It renders WORKFLOW.md as an
n8n-style flow of step nodes; clicking a node lets you edit its body, toggle the
skills required at that step, and edit those skills' content — all saved back to
`harn_env/` files. Pure stdlib (`http.server` + a single self-contained HTML page
with vanilla JS), in keeping with harn's lean footprint.

The HTTP layer is thin; the real work is three pure functions —
`state_payload`, `apply_workflow`, `apply_skill` — so they're unit-testable
without binding a socket.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import skills as skills_mod
from . import workflow as workflow_mod


# --------------------------------------------------------------------------- #
# Pure data layer (no HTTP) — easy to unit-test
# --------------------------------------------------------------------------- #
_LAYOUT_FILE = "studio_layout.json"


def _layout_path(env_dir: Path) -> Path:
    return env_dir / "state" / _LAYOUT_FILE


def layout_payload(env_dir: Path) -> dict:
    """Node positions for the canvas, keyed by node title → {x, y}. UI-only
    metadata kept OUT of WORKFLOW.md (the agent never needs coordinates), in
    harn_env/state/studio_layout.json. Missing/corrupt → empty (auto-layout)."""
    p = _layout_path(env_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def apply_layout(env_dir: Path, payload: dict) -> dict:
    """Persist node positions from a drag/drop. Best-effort; ignores bad input."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "expected an object"}
    clean: dict = {}
    for title, pos in payload.items():
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            try:
                clean[str(title)] = {"x": round(float(pos["x"])),
                                     "y": round(float(pos["y"]))}
            except (TypeError, ValueError):
                continue
    p = _layout_path(env_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return {"ok": True}


def state_payload(env_dir: Path) -> dict:
    """Everything the editor needs: the parsed workflow nodes + every skill
    (name, description, body) + saved canvas positions."""
    parsed = workflow_mod.parse(env_dir)
    sk = []
    for s in skills_mod.discover(env_dir):
        sk.append({"name": s.name, "description": s.description, "body": s.body()})
    return {"workflow": parsed, "skills": sk, "layout": layout_payload(env_dir)}


def apply_workflow(env_dir: Path, payload: dict) -> dict:
    """Persist edited workflow nodes back to WORKFLOW.md."""
    workflow_mod.save_parsed(env_dir, {
        "preamble": payload.get("preamble", ""),
        "nodes": payload.get("nodes", []),
    })
    return {"ok": True}


def apply_skill(env_dir: Path, payload: dict) -> dict:
    """Persist one edited (or new) skill body."""
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing skill name"}
    skills_mod.write_skill_body(env_dir, name, payload.get("body", ""),
                                payload.get("description"))
    return {"ok": True}


def delete_skill(env_dir: Path, payload: dict) -> dict:
    """Remove a skill (its SKILL.md directory). The workflow may still reference
    the name harmlessly; the editor also strips it from steps on the next save."""
    import shutil
    name = (payload.get("name") or "").strip().lower().replace(" ", "-")
    if not name:
        return {"ok": False, "error": "missing skill name"}
    d = env_dir / "skills" / name
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
def _make_handler(env_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(state_payload(env_dir))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/workflow":
                self._json(apply_workflow(env_dir, self._read_json()))
            elif self.path == "/api/skill":
                self._json(apply_skill(env_dir, self._read_json()))
            elif self.path == "/api/skill/delete":
                self._json(delete_skill(env_dir, self._read_json()))
            elif self.path == "/api/layout":
                self._json(apply_layout(env_dir, self._read_json()))
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(env_dir: Path, *, host: str = "127.0.0.1", port: int = 9999,
          open_browser: bool = True) -> None:
    """Block serving the studio until Ctrl-C."""
    workflow_mod.write(env_dir)  # ensure WORKFLOW.md exists
    httpd = ThreadingHTTPServer((host, port), _make_handler(env_dir))
    url = f"http://{host}:{port}"
    print(f"[harn] studio at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[harn] studio stopped.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# The single-page app (vanilla JS, self-contained)
# --------------------------------------------------------------------------- #
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>harn studio</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1d212b; --line:#2a2f3a;
    --text:#e6e9ef; --muted:#9aa3b2; --accent:#7c8cff; --accent2:#3ad6a0;
    --chip:#262b36; --chipOn:#2b3a5e; --insp-w:400px;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
  header{display:flex;align-items:center;gap:14px;padding:10px 16px;
    background:var(--panel);border-bottom:1px solid var(--line)}
  header h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.3px}
  header .dot{width:8px;height:8px;border-radius:50%;background:var(--accent2)}
  header .sp{flex:1}
  button{font:inherit;border:1px solid var(--line);background:var(--panel2);
    color:var(--text);padding:7px 12px;border-radius:8px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#0b0d12;font-weight:600}
  .tabs{display:flex;gap:6px}
  .tabs button.active{border-color:var(--accent);color:var(--accent)}
  .status{color:var(--muted);font-size:12px;min-width:120px;text-align:right}
  main{display:grid;grid-template-columns:1fr 6px var(--insp-w);height:calc(100vh - 53px)}
  .canvas{position:relative;overflow:auto;background:
    radial-gradient(circle at 1px 1px,#222732 1px,transparent 0) 0 0/24px 24px var(--bg)}
  .canvas.list{overflow:auto}
  .surface{position:relative;width:2000px;height:1500px;transform-origin:0 0}
  .listview{display:none;padding:18px 26px;max-width:760px}
  .listview h2{color:var(--muted);font-size:13px;letter-spacing:.6px;margin:0 0 10px}
  .zoom{position:absolute;right:14px;bottom:10px;display:flex;align-items:center;gap:2px;
    background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:2px;z-index:9}
  .zoom button{padding:2px 9px;border:none;background:transparent}
  .zoom span{font-size:11px;color:var(--muted);min-width:42px;text-align:center;cursor:pointer}
  svg.edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
  .edge{fill:none;stroke:#3a4150;stroke-width:2}
  .node{position:absolute;width:260px;background:var(--panel);border:1px solid var(--line);
    border-radius:12px;padding:12px 14px;cursor:grab;user-select:none;z-index:1;
    box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .node:hover{border-color:#3a4254}
  .node.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 4px 14px rgba(0,0,0,.35)}
  .node.drag{cursor:grabbing;z-index:5;opacity:.96}
  .node.off{opacity:.5;border-style:dashed}
  .node.off .ttl span:last-child{text-decoration:line-through}
  .nbtn{position:absolute;top:8px;right:8px;width:22px;height:22px;border-radius:6px;
    border:1px solid var(--line);background:var(--panel2);color:var(--muted);
    display:flex;align-items:center;justify-content:center;font-size:12px;cursor:pointer;z-index:2}
  .nbtn:hover{border-color:var(--accent);color:var(--text)}
  .nbtn.on{color:var(--accent2);border-color:#2f5a48}
  .node .ttl{font-weight:600;font-size:13.5px;display:flex;align-items:center;gap:8px;padding-right:24px}
  .node .num{width:22px;height:22px;border-radius:6px;background:var(--panel2);
    border:1px solid var(--line);display:flex;align-items:center;justify-content:center;
    font-size:11px;color:var(--muted);flex:0 0 auto}
  .node .chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
  .chip{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--chip);
    border:1px solid var(--line);color:var(--muted)}
  .chip.req{background:var(--chipOn);border-color:#3a4f7a;color:#cdd7f5}
  .node .tools{margin-top:7px;font-size:11px;color:var(--muted);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .grip{width:6px;cursor:col-resize;background:var(--line)}
  .grip:hover,.grip.act{background:var(--accent)}
  .insp{background:var(--panel);overflow:auto;padding:16px}
  .insp h2{font-size:13px;margin:0 0 4px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.6px;font-weight:600}
  label{display:block;font-size:12px;color:var(--muted);margin:14px 0 5px}
  input[type=text],textarea{width:100%;background:var(--panel2);color:var(--text);
    border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}
  textarea{resize:vertical;min-height:90px;font-size:13px}
  .skillgrid{display:flex;flex-wrap:wrap;gap:6px}
  .tog{font-size:12px;padding:4px 9px;border-radius:8px;cursor:pointer;
    background:var(--chip);border:1px solid var(--line);color:var(--muted);user-select:none}
  .tog.on{background:var(--chipOn);border-color:#3a4f7a;color:#cdd7f5}
  .empty{color:var(--muted);padding:40px 10px;text-align:center}
  .skillrow{display:flex;align-items:center;gap:8px;padding:8px 6px;border-bottom:1px solid var(--line);cursor:pointer}
  .skillrow:hover{background:var(--panel2)}
  .skillrow .nm{font-weight:600;font-size:13px}
  .skillrow .ds{color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row{display:flex;gap:8px;align-items:center}
  .mut{color:var(--muted);font-size:12px}
  a.link{color:var(--accent);cursor:pointer;text-decoration:none;font-size:12px}
  a.link:hover{text-decoration:underline}
  .hint{position:absolute;left:14px;bottom:10px;font-size:11px;color:var(--muted);
    background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:5px 9px;z-index:9}
  /* Notion-like rendered markdown */
  .md{font-size:13.5px;line-height:1.65;color:var(--text)}
  .md>:first-child{margin-top:0}
  .md h1,.md h2,.md h3,.md h4{font-weight:600;line-height:1.3;margin:16px 0 6px}
  .md h1{font-size:19px} .md h2{font-size:16px} .md h3{font-size:14px} .md h4{font-size:13px}
  .md p{margin:7px 0}
  .md ul,.md ol{margin:7px 0;padding-left:20px}
  .md li{margin:3px 0}
  .md code{background:var(--panel2);border:1px solid var(--line);border-radius:5px;
    padding:1px 5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  .md pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
    padding:11px 13px;overflow:auto;margin:9px 0}
  .md pre code{background:none;border:none;padding:0;font-size:12px}
  .md blockquote{border-left:3px solid var(--accent);margin:9px 0;padding:2px 0 2px 13px;color:var(--muted)}
  .md a{color:var(--accent)}
  .md hr{border:none;border-top:1px solid var(--line);margin:13px 0}
  .md strong{font-weight:600}
  .mdview{cursor:text;border-radius:8px;padding:9px 11px;border:1px solid transparent;min-height:42px}
  .mdview:hover{border-color:var(--line);background:var(--panel2)}
  .mdview.empty{color:var(--muted)}
</style>
</head>
<body>
<header>
  <span class="dot"></span><h1>harn studio</h1>
  <div class="tabs">
    <button id="tabFlow" class="active" onclick="showTab('flow')">Flow</button>
    <button id="tabSkills" onclick="showTab('skills')">Skills</button>
    <button id="tabTools" onclick="showTab('tools')">Tools</button>
  </div>
  <span class="sp"></span>
  <span class="status" id="status">loading…</span>
  <button onclick="addStep()" id="addBtn">＋ Add step</button>
  <button onclick="addSkill()" id="addSkillBtn" style="display:none">＋ Add skill</button>
  <button onclick="autoArrange()" id="arrangeBtn">Auto-arrange</button>
  <button class="primary" id="saveBtn" onclick="saveFlow()">Save flow</button>
</header>
<main>
  <div class="canvas" id="canvas">
    <div class="surface" id="surface">
      <svg class="edges" id="edges"></svg>
    </div>
    <div class="listview" id="listView"></div>
    <div class="hint" id="hint">drag blocks · drag empty space to pan · pinch or ± to zoom</div>
    <div class="zoom" id="zoom"><button onclick="zoomBy(1/1.2)">−</button>
      <span id="zlbl" onclick="zoomReset()">100%</span>
      <button onclick="zoomBy(1.2)">＋</button></div>
  </div>
  <div class="grip" id="grip"></div>
  <div class="insp" id="insp"><div class="empty">Select a node to edit it.</div></div>
</main>
<script>
const $=s=>document.querySelector(s);
let S={workflow:{preamble:"",nodes:[]},skills:[],layout:{}};
let L={};                 // title -> {x,y}
let selNode=null, tab='flow', dirty=false, skillSel=-1, bodyMode='preview';

function setStatus(t){ $('#status').textContent=t; }
function markDirty(){ dirty=true; setStatus('unsaved changes'); }

async function load(){
  const r=await fetch('/api/state'); S=await r.json();
  L=Object.assign({}, S.layout||{});
  setStatus(S.skills.length+' skills · '+S.workflow.nodes.filter(n=>n.kind==='step').length+' steps');
  render();
}
function skillNames(){ return S.skills.map(s=>s.name); }
function allTools(){ const s=new Set(); S.workflow.nodes.forEach(n=>(n.tools||[]).forEach(t=>s.add(t))); return [...s].sort(); }
function render(){ if(tab==='flow')renderFlow(); else if(tab==='skills')renderSkills(); else renderTools(); }
let Z=1;   // canvas zoom

/* numbers follow array order: each enabled or disabled STEP gets the next n */
function numbers(){ let c=0; return S.workflow.nodes.map(n=> n.kind==='step' ? (++c) : null); }
function uniqueTitle(base){ let t=base,i=2; const has=x=>S.workflow.nodes.some(n=>n.title===x);
  while(has(t)){ t=base+' '+i; i++; } return t; }

/* ---------- flow canvas ---------- */
function posFor(n,i){ return L[n.title] || {x:120, y:40+i*170}; }
function renderFlow(){
  $('#hint').style.display='';
  const surf=$('#surface');
  [...surf.querySelectorAll('.node')].forEach(e=>e.remove());
  const nums=numbers();
  S.workflow.nodes.forEach((n,i)=>{
    const p=posFor(n,i); L[n.title]=p;
    const el=document.createElement('div');
    el.className='node'+(n===selNode?' sel':'')+(n.enabled===false?' off':''); el.dataset.i=i;
    el.style.left=p.x+'px'; el.style.top=p.y+'px';
    const num = nums[i]!=null ? nums[i] : '•';
    const reqChips=(n.required||[]).map(s=>`<span class="chip req">${esc(s)}</span>`).join('');
    const tools=(n.tools||[]).length?`<div class="tools">⚙ ${(n.tools||[]).map(esc).join(', ')}</div>`:'';
    const onoff=n.kind==='step'
      ? `<div class="nbtn ${n.enabled!==false?'on':''}" title="enable/disable"
           onclick="toggleEnabled(${i});event.stopPropagation()">${n.enabled!==false?'●':'○'}</div>` : '';
    el.innerHTML=`${onoff}<div class="ttl"><span class="num">${esc(num)}</span><span>${esc(n.title)}</span></div>
      <div class="chips">${reqChips|| (n.kind==='step'?'<span class="chip">no required skills</span>':'')}</div>${tools}`;
    surf.appendChild(el);
  });
  fitSurface(); redrawEdges();
  renderInsp();
}
function fitSurface(){
  let mx=1200,my=900;
  document.querySelectorAll('.node').forEach(e=>{
    mx=Math.max(mx,e.offsetLeft+e.offsetWidth+200);
    my=Math.max(my,e.offsetTop+e.offsetHeight+200);
  });
  const s=$('#surface'); s.style.width=mx+'px'; s.style.height=my+'px';
}
function redrawEdges(){
  const nodes=[...document.querySelectorAll('.node')].sort((a,b)=>a.dataset.i-b.dataset.i);
  let d='';
  for(let i=0;i<nodes.length-1;i++){
    const a=nodes[i], b=nodes[i+1];
    const x1=a.offsetLeft+a.offsetWidth/2, y1=a.offsetTop+a.offsetHeight;
    const x2=b.offsetLeft+b.offsetWidth/2, y2=b.offsetTop;
    const dy=Math.max(30,Math.abs(y2-y1)/2);
    d+=`<path class="edge" d="M${x1} ${y1} C ${x1} ${y1+dy} ${x2} ${y2-dy} ${x2} ${y2}"/>`
      +`<circle cx="${x2}" cy="${y2}" r="3" fill="#3a4150"/>`;
  }
  $('#edges').innerHTML=d;
}
/* order = top-to-bottom, then left-to-right on the canvas → drag to reorder */
function resortByPosition(){
  S.workflow.nodes.sort((a,b)=>{
    const pa=L[a.title]||{x:1e9,y:1e9}, pb=L[b.title]||{x:1e9,y:1e9};
    return (pa.y-pb.y)||(pa.x-pb.x);
  });
}
function autoArrange(){
  resortByPosition();
  S.workflow.nodes.forEach((n,i)=>{ L[n.title]={x:120,y:40+i*170}; });
  renderFlow(); saveLayout(); markDirty();
}
function addStep(){
  let my=40; document.querySelectorAll('.node').forEach(e=>my=Math.max(my,e.offsetTop+e.offsetHeight));
  const n={title:uniqueTitle('New step'),body:'',required:[],tools:[],kind:'step',enabled:true};
  L[n.title]={x:120,y:my+50};
  S.workflow.nodes.push(n); selNode=n; bodyMode='write'; markDirty(); renderFlow(); saveLayout();
}
function toggleEnabled(i){ const n=S.workflow.nodes[i]; n.enabled=n.enabled===false; markDirty(); renderFlow(); }
function deleteNode(){
  if(!selNode) return;
  const i=S.workflow.nodes.indexOf(selNode); if(i<0) return;
  delete L[selNode.title]; S.workflow.nodes.splice(i,1); selNode=null;
  markDirty(); renderFlow(); saveLayout();
}

/* ---------- drag nodes + pan canvas ---------- */
let drag=null, pan=null;
$('#surface').addEventListener('pointerdown',e=>{
  if(e.target.closest('.nbtn,button,input,textarea,a,.tog')) return;
  const node=e.target.closest('.node');
  if(node){
    const i=+node.dataset.i; selNode=S.workflow.nodes[i]; bodyMode='preview'; highlight(); renderInsp();
    drag={el:node,title:selNode.title,sx:e.clientX,sy:e.clientY,
          ox:node.offsetLeft,oy:node.offsetTop,moved:false};
    node.classList.add('drag'); node.setPointerCapture(e.pointerId);
    e.preventDefault();
  }
});
$('#canvas').addEventListener('pointerdown',e=>{
  if(e.target.closest('.node')) return;
  const c=$('#canvas');
  pan={sx:e.clientX,sy:e.clientY,sl:c.scrollLeft,st:c.scrollTop};
  c.style.cursor='grabbing';
});
window.addEventListener('pointermove',e=>{
  if(drag){
    const nx=Math.max(0,drag.ox+(e.clientX-drag.sx)/Z);
    const ny=Math.max(0,drag.oy+(e.clientY-drag.sy)/Z);
    drag.el.style.left=nx+'px'; drag.el.style.top=ny+'px';
    L[drag.title]={x:nx,y:ny}; drag.moved=true; redrawEdges();
  }else if(pan){
    const c=$('#canvas');
    c.scrollLeft=pan.sl-(e.clientX-pan.sx);
    c.scrollTop =pan.st-(e.clientY-pan.sy);
  }
});
window.addEventListener('pointerup',()=>{
  if(drag){
    drag.el.classList.remove('drag');
    if(drag.moved){ resortByPosition(); renderFlow(); saveLayout(); markDirty(); } // reorder → renumber
    drag=null;
  }
  if(pan){ $('#canvas').style.cursor=''; pan=null; }
});
function highlight(){ document.querySelectorAll('.node').forEach(e=>
  e.classList.toggle('sel', S.workflow.nodes[+e.dataset.i]===selNode)); }
async function saveLayout(){
  await fetch('/api/layout',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(L)});
}

/* ---------- zoom ---------- */
function applyZoom(){ $('#surface').style.transform='scale('+Z+')';
  $('#zlbl').textContent=Math.round(Z*100)+'%'; }
function setZoom(z, cx, cy){
  z=Math.max(0.4,Math.min(2,z)); if(z===Z) return;
  const c=$('#canvas'), r=c.getBoundingClientRect();
  cx=cx==null? r.width/2 : cx-r.left; cy=cy==null? r.height/2 : cy-r.top;
  const px=(c.scrollLeft+cx)/Z, py=(c.scrollTop+cy)/Z;   // surface point under cursor
  Z=z; applyZoom();
  c.scrollLeft=px*Z-cx; c.scrollTop=py*Z-cy;             // keep that point fixed
}
function zoomBy(f){ setZoom(Z*f); }
function zoomReset(){ setZoom(1); }
$('#canvas').addEventListener('wheel',e=>{
  if(tab!=='flow') return;
  // Plain wheel / two-finger scroll → let the canvas scroll natively (no zoom).
  // Trackpad PINCH arrives as a wheel event with ctrlKey set → that zooms.
  if(!e.ctrlKey) return;
  e.preventDefault();
  setZoom(Z*Math.exp(-e.deltaY*0.01), e.clientX, e.clientY);
},{passive:false});

/* ---------- markdown rendering (Notion-like, dependency-free) ---------- */
function mdToHtml(src){
  if(!src||!src.trim()) return '';
  const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const blocks=[];
  src=src.replace(/```([\s\S]*?)```/g,(m,c)=>{ blocks.push('<pre><code>'+esc(c.replace(/^\n/,''))+'</code></pre>'); return ''+(blocks.length-1)+''; });
  const inline=t=>{ t=esc(t);
    t=t.replace(/`([^`]+)`/g,(m,c)=>'<code>'+c+'</code>');
    t=t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    t=t.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
    t=t.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(m,x,u)=>{ u=u.trim(); if(/^javascript:/i.test(u))u='#';
      return '<a href="'+u+'" target="_blank" rel="noopener">'+x+'</a>'; });
    return t; };
  const lines=src.split('\n'), out=[]; let i=0, para=[];
  const flush=()=>{ if(para.length){ out.push('<p>'+para.map(inline).join('<br>')+'</p>'); para=[]; } };
  while(i<lines.length){ const ln=lines[i];
    const ph=ln.match(/^(\d+)$/);
    if(ph){ flush(); out.push(blocks[+ph[1]]); i++; continue; }
    if(/^\s*$/.test(ln)){ flush(); i++; continue; }
    const h=ln.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flush(); const l=h[1].length; out.push('<h'+l+'>'+inline(h[2])+'</h'+l+'>'); i++; continue; }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(ln)){ flush(); out.push('<hr>'); i++; continue; }
    if(/^\s*>\s?/.test(ln)){ flush(); const q=[]; while(i<lines.length&&/^\s*>\s?/.test(lines[i])){ q.push(lines[i].replace(/^\s*>\s?/,'')); i++; } out.push('<blockquote>'+q.map(inline).join('<br>')+'</blockquote>'); continue; }
    if(/^\s*[-*+]\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*[-*+]\s+/,''))+'</li>'); i++; } out.push('<ul>'+it.join('')+'</ul>'); continue; }
    if(/^\s*\d+\.\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*\d+\.\s+/,''))+'</li>'); i++; } out.push('<ol>'+it.join('')+'</ol>'); continue; }
    para.push(ln); i++;
  }
  flush();
  return out.join('\n');
}
/* one body editor at a time (the selected node OR skill) — click to edit, blur to render */
function getBody(){ return tab==='flow' ? (selNode?selNode.body||'':'') : (S.skills[skillSel]?S.skills[skillSel].body||'':''); }
function setBody(v){ if(tab==='flow'){ if(selNode){selNode.body=v; markDirty();} } else if(S.skills[skillSel]){ S.skills[skillSel].body=v; } }
function reInsp(){ tab==='flow'?renderInsp():renderSkillEditor(); }
function bodyHtml(minH){ minH=minH||140;
  if(bodyMode==='write')
    return `<textarea id="bodyTA" style="min-height:${minH}px" oninput="setBody(this.value)" onblur="doneBody()">${esc(getBody())}</textarea>`;
  const html=mdToHtml(getBody());
  return `<div class="mdview ${html?'':'empty'}" style="min-height:${minH}px" onclick="editBody()" title="click to edit">`
    + (html?`<div class="md">${html}</div>`:'click to write…') + `</div>`;
}
function editBody(){ bodyMode='write'; reInsp();
  const t=$('#bodyTA'); if(t){ t.focus(); t.setSelectionRange(t.value.length,t.value.length); } }
function doneBody(){ bodyMode='preview'; reInsp(); }

/* ---------- inspector ---------- */
function renderInsp(){
  if(tab==='skills'){ renderSkillEditor(); return; }
  const n=selNode; if(!n){ $('#insp').innerHTML='<div class="empty">Select a node to edit it.</div>'; return; }
  const isStep=n.kind==='step';
  const toggles=skillNames().map(name=>{
    const on=(n.required||[]).includes(name);
    return `<span class="tog ${on?'on':''}" onclick="toggleReq('${esc(name)}')">${esc(name)}</span>`;
  }).join('');
  const reqLinks=(n.required||[]).map(name=>`<a class="link" onclick="editSkill('${esc(name)}')">edit ${esc(name)} »</a>`).join(' · ');
  const toolTogs=allTools().map(t=>{
    const on=(n.tools||[]).includes(t);
    return `<span class="tog ${on?'on':''}" onclick="toggleTool('${esc(t)}')">${esc(t)}</span>`;
  }).join('');
  $('#insp').innerHTML=`
    <h2>${isStep?'Step':'Note'}</h2>
    <div class="row" style="justify-content:space-between">
      <label style="margin:0">${isStep?'<input type="checkbox" '+(n.enabled!==false?'checked':'')+' onchange="setEnabled(this.checked)"/> enabled':''}</label>
      <a class="link" style="color:var(--danger)" onclick="deleteNode()">delete »</a>
    </div>
    <label>Title</label>
    <input type="text" value="${esc(n.title)}" oninput="upd('title',this.value)"/>
    <label>Description / steps</label>
    ${bodyHtml(140)}
    ${isStep?`
    <label>Skills required at this step <span class="mut">(click to toggle)</span></label>
    <div class="skillgrid">${toggles||'<span class="mut">no skills yet</span>'}</div>
    <div style="margin-top:8px">${reqLinks}</div>
    <label>Tools at this step <span class="mut">(click to toggle · add below)</span></label>
    <div class="skillgrid">${toolTogs||'<span class="mut">no tools yet</span>'}</div>
    <input type="text" placeholder="add a tool, press Enter" style="margin-top:8px"
      onkeydown="if(event.key==='Enter'){addTool(this.value);this.value='';}"/>`:''}
  `;
}
function upd(k,v){
  if(!selNode) return; const old=selNode.title;
  selNode[k]=v; markDirty();
  if(k==='title'){ if(L[old]){ L[v]=L[old]; if(v!==old) delete L[old]; } renderFlow(); }
}
function setEnabled(on){ if(!selNode)return; selNode.enabled=on; markDirty(); renderFlow(); }
function toggleReq(name){
  if(!selNode)return; selNode.required=selNode.required||[];
  const k=selNode.required.indexOf(name); if(k>=0)selNode.required.splice(k,1); else selNode.required.push(name);
  markDirty(); renderFlow();
}
function toggleTool(name){
  if(!selNode)return; selNode.tools=selNode.tools||[];
  const k=selNode.tools.indexOf(name); if(k>=0)selNode.tools.splice(k,1); else selNode.tools.push(name);
  markDirty(); renderFlow();
}
function addTool(v){ v=(v||'').trim(); if(!v||!selNode)return;
  selNode.tools=selNode.tools||[]; if(!selNode.tools.includes(v))selNode.tools.push(v);
  markDirty(); renderFlow(); }
async function saveFlow(){
  setStatus('saving…'); resortByPosition();
  const r=await fetch('/api/workflow',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(S.workflow)});
  const j=await r.json(); dirty=false; setStatus(j.ok?'saved ✓':'save failed'); renderFlow();
}

/* ---------- tabs + list views ---------- */
function showTab(t){ tab=t; bodyMode='preview';
  ['flow','skills','tools'].forEach(x=>$('#tab'+x[0].toUpperCase()+x.slice(1)).classList.toggle('active',x===t));
  $('#surface').style.display = t==='flow'?'':'none';
  $('#listView').style.display = t==='flow'?'none':'block';
  $('#hint').style.display = t==='flow'?'':'none';
  $('#zoom').style.display = t==='flow'?'':'none';
  $('#canvas').classList.toggle('list',t!=='flow');
  $('#saveBtn').style.display=t==='flow'?'':'none';
  $('#arrangeBtn').style.display=t==='flow'?'':'none';
  $('#addBtn').style.display=t==='flow'?'':'none';
  $('#addSkillBtn').style.display=t==='skills'?'':'none';
  if(t==='flow')renderFlow(); else if(t==='skills')renderSkills(); else renderTools();
}

/* ---------- skills tab ---------- */
function renderSkills(){
  const v=$('#listView'); v.innerHTML='<h2>SKILLS</h2>';
  S.skills.forEach((s,i)=>{ const r=document.createElement('div');r.className='skillrow';
    r.onclick=()=>{skillSel=i;bodyMode='preview';renderSkillEditor();};
    r.innerHTML=`<div><div class="nm">${esc(s.name)}</div><div class="ds">${esc(s.description||'')}</div></div>`;
    v.appendChild(r); });
  if(!S.skills.length) v.innerHTML+='<div class="empty">No skills yet — “＋ Add skill”.</div>';
  if(skillSel>=0&&skillSel<S.skills.length) renderSkillEditor();
  else $('#insp').innerHTML='<div class="empty">Select a skill to edit it.</div>';
}
function editSkill(name){ showTab('skills'); skillSel=S.skills.findIndex(s=>s.name===name);
  renderSkills(); renderSkillEditor(); }
function addSkill(){
  let base='new-skill',n=base,i=2; while(S.skills.some(s=>s.name===n)){ n=base+'-'+i;i++; }
  S.skills.push({name:n,description:'',body:'# '+n+'\n\n'}); skillSel=S.skills.length-1;
  bodyMode='write'; renderSkills(); renderSkillEditor();
}
function renderSkillEditor(){
  const s=S.skills[skillSel]; if(!s){ $('#insp').innerHTML='<div class="empty">Select a skill.</div>'; return; }
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Skill</h2>
      <a class="link" style="color:var(--danger)" onclick="deleteSkill(${skillSel})">delete »</a>
    </div>
    <label>Name <span class="mut">(slug)</span></label>
    <input type="text" value="${esc(s.name)}" oninput="S.skills[${skillSel}].name=this.value.trim().toLowerCase().replace(/\s+/g,'-')"/>
    <label>Description</label>
    <input type="text" value="${esc(s.description||'')}" oninput="S.skills[${skillSel}].description=this.value"/>
    <label>Body (Markdown)</label>
    ${bodyHtml(300)}
    <div class="row" style="margin-top:12px">
      <button class="primary" onclick="saveSkill(${skillSel})">Save skill</button>
      <span class="status" id="sst"></span>
    </div>`;
}
async function saveSkill(i){
  const s=S.skills[i]; if(!s.name){ $('#sst').textContent='name required'; return; }
  $('#sst').textContent='saving…';
  const r=await fetch('/api/skill',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:s.name,description:s.description,body:s.body})});
  const j=await r.json(); renderSkills();
  const st=$('#sst'); if(st) st.textContent=j.ok?'saved ✓':'failed';
}
async function deleteSkill(i){
  const s=S.skills[i]; if(!confirm('Delete skill "'+s.name+'"?'))return;
  await fetch('/api/skill/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:s.name})});
  // also strip it from every step's required
  S.workflow.nodes.forEach(n=>{ if(n.required){const k=n.required.indexOf(s.name);if(k>=0){n.required.splice(k,1);dirty=true;}} });
  S.skills.splice(i,1); skillSel=Math.min(skillSel,S.skills.length-1);
  $('#insp').innerHTML='<div class="empty">Skill deleted.</div>'; renderSkills();
}

/* ---------- tools tab (tools live in steps; edited via the workflow) ---------- */
let toolSel=null;
function renderTools(){
  const v=$('#listView'); const tools=allTools();
  v.innerHTML='<h2>TOOLS <span class="mut" style="text-transform:none;letter-spacing:0">— used across steps; Save flow to persist</span></h2>';
  tools.forEach(t=>{ const users=S.workflow.nodes.filter(n=>(n.tools||[]).includes(t));
    const r=document.createElement('div');r.className='skillrow';
    r.onclick=()=>{toolSel=t;renderToolEditor();};
    r.innerHTML=`<div><div class="nm">${esc(t)}</div><div class="ds">${users.length} step(s): ${esc(users.map(u=>u.title).join(', '))}</div></div>`;
    v.appendChild(r); });
  if(!tools.length) v.innerHTML+='<div class="empty">No tools yet — add tools on a step (Flow tab).</div>';
  if(toolSel&&tools.includes(toolSel)) renderToolEditor(); else $('#insp').innerHTML='<div class="empty">Select a tool.</div>';
}
function renderToolEditor(){
  const users=S.workflow.nodes.filter(n=>(n.tools||[]).includes(toolSel));
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Tool</h2>
      <a class="link" style="color:var(--danger)" onclick="deleteTool('${esc(toolSel)}')">remove from all »</a>
    </div>
    <label>Name <span class="mut">(rename across all steps)</span></label>
    <input type="text" value="${esc(toolSel)}" onchange="renameTool('${esc(toolSel)}',this.value)"/>
    <label>Used by ${users.length} step(s)</label>
    <div class="skillgrid">${users.map(u=>`<span class="chip">${esc(u.title)}</span>`).join('')||'<span class="mut">none</span>'}</div>
    <div class="mut" style="margin-top:14px">Tools are part of the workflow — click <b>Save flow</b> on the Flow tab to persist renames/removals.</div>`;
}
function renameTool(oldn,newn){ newn=(newn||'').trim(); if(!newn||newn===oldn){renderTools();return;}
  S.workflow.nodes.forEach(n=>{ if(n.tools){ const k=n.tools.indexOf(oldn); if(k>=0)n.tools[k]=newn; } });
  toolSel=newn; markDirty(); renderTools(); }
function deleteTool(t){ if(!confirm('Remove tool "'+t+'" from all steps?'))return;
  S.workflow.nodes.forEach(n=>{ if(n.tools){ const k=n.tools.indexOf(t); if(k>=0)n.tools.splice(k,1); } });
  toolSel=null; markDirty(); renderTools(); }

/* ---------- resizable inspector ---------- */
let rs=null;
$('#grip').addEventListener('pointerdown',e=>{
  rs={sx:e.clientX,w:parseInt(getComputedStyle(document.documentElement).getPropertyValue('--insp-w'))};
  $('#grip').classList.add('act'); $('#grip').setPointerCapture(e.pointerId); e.preventDefault();
});
window.addEventListener('pointermove',e=>{
  if(!rs)return;
  let w=rs.w-(e.clientX-rs.sx); w=Math.max(300,Math.min(820,w));
  document.documentElement.style.setProperty('--insp-w',w+'px');
});
window.addEventListener('pointerup',()=>{ if(rs){ $('#grip').classList.remove('act'); rs=null; } });

function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
window.addEventListener('beforeunload',e=>{ if(dirty){e.preventDefault();e.returnValue='';} });
load();
</script>
</body>
</html>
"""
