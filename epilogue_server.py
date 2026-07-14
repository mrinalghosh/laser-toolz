#!/usr/bin/env python3
"""
epilogue_server.py — minimal web UI for epilogue.py.

A thin single-user Flask wrapper (same spirit as server.py / segment_server.py)
around epilogue's SVG preflight. Drop an SVG, flip the optimization toggles, and
see the flattened, true-scale result live with a before/after compare and a
colour / operation audit. Every toggle carries a tooltip explaining what it does.

Because epilogue is pure-Python and instant (no model), the preview re-renders on
every change — no explicit "render" button.

  python epilogue_server.py            # http://127.0.0.1:5003
  PORT=8080 python epilogue_server.py
"""

from __future__ import annotations

import io
import os
import secrets

from flask import Flask, jsonify, request, send_file

from epilogue import EpiParams, svg_to_epilog
from toolz_nav import nav_html

app = Flask(__name__)

# Single-user, in-memory, cleared on restart. token -> raw uploaded SVG bytes.
_SVGS: dict[str, bytes] = {}
_NAMES: dict[str, str] = {}
_MAX = 24


def _params_from_json(d) -> EpiParams:
    """Reconstruct EpiParams from the front-end JSON. width_mm is float-or-None
    (blank = read from file); dpi is float; hairline/snap are bools; the rest are
    strings — mirrors linify/segment's explicit type handling."""
    w = d.get("width_mm")
    width_mm = float(w) if (w not in (None, "",) ) else None
    return EpiParams(
        width_mm=width_mm,
        units=d.get("units", "mm"),
        dpi=float(d.get("dpi") or 96),
        hairline=bool(d.get("hairline", True)),
        stroke_width=str(d.get("stroke_width", "0.02")),
        color=d.get("color") or "#000000",
        snap_colors=bool(d.get("snap_colors", False)),
    )


@app.route("/")
def index():
    return _PAGE.replace("<!--TOOLZ-NAV-->", nav_html("epilogue"))


@app.post("/upload")
def upload():
    f = request.files.get("svg")
    if not f:
        return jsonify(error="no file"), 400
    raw = f.read()
    tok = secrets.token_urlsafe(8)
    if len(_SVGS) >= _MAX:                       # evict oldest (dict is insertion-ordered)
        old = next(iter(_SVGS))
        _SVGS.pop(old, None)
        _NAMES.pop(old, None)
    _SVGS[tok] = raw
    _NAMES[tok] = f.filename or "drawing.svg"
    return jsonify(id=tok, name=_NAMES[tok],
                   svg=raw.decode("utf-8", "replace"))   # raw text for before-view


@app.post("/render")
def render():
    d = request.get_json(force=True)
    raw = _SVGS.get(d.get("id"))
    if raw is None:
        return jsonify(error="unknown id — re-upload"), 404
    try:
        svg, stats = svg_to_epilog(raw, _params_from_json(d))
    except Exception as exc:                     # surface parse/scale errors in the UI
        return jsonify(error=str(exc) or type(exc).__name__)
    return jsonify(svg=svg, stats=stats)


@app.post("/download")
def download():
    d = request.get_json(force=True)
    raw = _SVGS.get(d.get("id"))
    if raw is None:
        return jsonify(error="unknown id"), 404
    svg, _ = svg_to_epilog(raw, _params_from_json(d))
    stem = _NAMES.get(d.get("id"), "drawing.svg").rsplit(".", 1)[0]
    return send_file(io.BytesIO(svg.encode("utf-8")), mimetype="image/svg+xml",
                     as_attachment=True, download_name=f"{stem}_epilog.svg")


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>epilogue · laser-toolz</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><g fill='none' stroke='%23ff453a' stroke-width='5' stroke-linecap='round'><path d='M14 20h36M20 32h24M26 44h12'/></g></svg>">
<style>
  :root {
    --bg:#0a0b0d; --panel:#111318; --stage:#050506; --line:#23262d;
    --fg:#e7e9ed; --mut:#767c87; --faint:#171a20; --field:#0d0f13;
    --acc:#ff453a; --acc-fg:#ffffff; --err:#ff6b5e; --ok:#31d158; --info:#0a84ff;
    --paper:#f7f7f4; --shadow:0 18px 60px rgba(0,0,0,.6);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f1f1ef; --panel:#ffffff; --stage:#e6e6e3; --line:#e0e0dc;
      --fg:#17181b; --mut:#83868d; --faint:#f5f5f3; --field:#fbfbfa;
      --acc:#e5322a; --acc-fg:#ffffff; --err:#c0392b; --ok:#1a9e3e; --info:#0a66d6;
      --paper:#ffffff; --shadow:0 12px 44px rgba(20,20,25,.14);
    }
  }
  * { box-sizing:border-box; }
  .hide { display:none !important; }
  html,body { height:100%; }
  body { margin:0; color:var(--fg); background:var(--bg); display:flex; flex-direction:column;
         font:13px/1.5 var(--sans); -webkit-font-smoothing:antialiased; }

  header { display:flex; align-items:center; gap:12px; padding:13px 22px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  .brand { margin:0; display:inline-flex; flex-direction:column; align-items:stretch; gap:3px; }
  .vec { display:flex; align-items:center; height:8px; padding:0 1px; }
  .vec .shaft { flex:1; height:1.5px; background:var(--acc); border-radius:1px; }
  .vec .head { width:0; height:0; margin-left:-1px; border-left:6px solid var(--acc);
               border-top:4px solid transparent; border-bottom:4px solid transparent; }
  .word { font-family:var(--mono); font-size:16px; font-weight:600; letter-spacing:-.01em; line-height:1; }
  .word .thin { color:var(--mut); font-weight:400; }
  .chip { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.14em;
          color:var(--mut); border:1px solid var(--line); border-radius:11px; padding:3px 9px; }
  .grow { flex:1; }

  .wrap { display:flex; flex:1; min-height:0; }
  .panel { width:322px; flex:none; border-right:1px solid var(--line); background:var(--panel);
           overflow-y:auto; padding:16px 18px 26px; }
  .panel::-webkit-scrollbar { width:9px; }
  .panel::-webkit-scrollbar-thumb { background:var(--line); border-radius:9px; border:3px solid var(--panel); }

  .stage { position:relative; flex:1; min-width:0; display:flex; align-items:center; justify-content:center;
           background:var(--stage); overflow:auto; padding:28px; }
  .paper { line-height:0; background:var(--paper); border-radius:2px; box-shadow:var(--shadow); }
  #out svg { display:block; }
  #out svg * { vector-effect:non-scaling-stroke; }         /* keep hairlines visible at any fit */
  #out.hair svg path { stroke-width:1.15px; }              /* preview-only: fatten cut strokes */
  .empty { color:var(--mut); padding:48px; font-size:12px; text-align:center; font-family:var(--mono); }

  /* before/after segmented toggle, floats over the stage */
  .seg { position:absolute; left:50%; top:14px; transform:translateX(-50%); display:flex; gap:0;
         background:color-mix(in srgb,var(--panel) 78%,transparent); backdrop-filter:blur(8px);
         border:1px solid var(--line); border-radius:8px; overflow:hidden; }
  .seg button { border:0; background:transparent; color:var(--mut); cursor:pointer; padding:5px 13px;
         font-family:var(--mono); font-size:11px; letter-spacing:.02em; }
  .seg button.on { background:var(--fg); color:var(--bg); }
  .stat { position:absolute; left:18px; bottom:14px; color:var(--mut); font-size:11px;
          font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:.02em;
          background:color-mix(in srgb,var(--panel) 78%,transparent); backdrop-filter:blur(8px);
          border:1px solid var(--line); border-radius:6px; padding:5px 10px; }
  .stat:empty { display:none; }

  .grp { margin-top:15px; }
  .grp:first-of-type { margin-top:2px; }
  .grp h3 { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.14em;
            color:var(--mut); margin:0 0 9px; font-weight:600; display:flex; align-items:center; gap:8px; }
  .grp h3::after { content:""; flex:1; height:1px; background:var(--line); }

  label.row { display:flex; align-items:center; gap:6px; margin:10px 0 3px; color:var(--fg);
              font-size:11.5px; font-family:var(--mono); letter-spacing:-.01em; }
  label.row:first-child { margin-top:0; }
  .hint { color:var(--mut); font-size:10.5px; margin:4px 0 0; font-family:var(--sans); }

  input[type=text], input[type=number], select { width:100%; background:var(--field); color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:6px 9px; font-family:var(--mono);
        font-size:12px; outline:none; transition:border-color .12s,box-shadow .12s; }
  select { appearance:none; cursor:pointer;
        background-image:linear-gradient(45deg,transparent 50%,var(--mut) 50%),linear-gradient(135deg,var(--mut) 50%,transparent 50%);
        background-position:calc(100% - 15px) 13px,calc(100% - 10px) 13px;
        background-size:5px 5px,5px 5px; background-repeat:no-repeat; padding-right:28px; }
  input:focus, select:focus { border-color:var(--acc);
        box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 20%,transparent); }
  input[type=number]::-webkit-outer-spin-button,input[type=number]::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }
  input[type=number] { -moz-appearance:textfield; }

  /* toggle switch (the optimization options) */
  .chk { display:flex; align-items:center; gap:9px; margin:12px 0 2px; font-size:11.5px; font-family:var(--mono); }
  .sw { position:relative; width:32px; height:18px; flex:none; }
  .sw input { position:absolute; opacity:0; width:100%; height:100%; margin:0; cursor:pointer; }
  .sw .tr { position:absolute; inset:0; background:var(--line); border-radius:9px; transition:background .14s; }
  .sw .tr::after { content:""; position:absolute; left:2px; top:2px; width:14px; height:14px; border-radius:50%;
        background:var(--paper); transition:transform .14s; }
  .sw input:checked + .tr { background:var(--acc); }
  .sw input:checked + .tr::after { transform:translateX(14px); }
  .chk label { cursor:pointer; }

  /* info tooltip trigger */
  .tip { display:inline-flex; align-items:center; justify-content:center; width:14px; height:14px; flex:none;
         border:1px solid var(--line); border-radius:50%; color:var(--mut); font:9px/1 var(--mono);
         cursor:help; user-select:none; }
  .tip:hover { color:var(--fg); border-color:var(--mut); }
  #tipbox { position:fixed; z-index:60; max-width:246px; background:var(--panel); color:var(--fg);
        border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:8px 11px;
        font:11px/1.45 var(--sans); opacity:0; visibility:hidden; transition:opacity .1s; pointer-events:none; }
  #tipbox.show { opacity:1; visibility:visible; }
  #tipbox code { font-family:var(--mono); font-size:10px; background:var(--faint);
        border:1px solid var(--line); border-radius:4px; padding:0 3px; }

  .act { width:100%; margin:7px 0 0; padding:8px 10px; border-radius:7px; border:1px solid var(--line);
         background:var(--field); color:var(--fg); cursor:pointer; font-family:var(--mono);
         font-size:12px; letter-spacing:.02em; transition:opacity .12s,transform .05s,border-color .12s,color .12s; }
  .act:hover:not(:disabled) { border-color:color-mix(in srgb,var(--acc) 45%,var(--line)); color:var(--fg); }
  .act:active:not(:disabled) { transform:translateY(1px); }
  .act:disabled { opacity:.4; cursor:not-allowed; }
  .act.prim { border-color:var(--fg); background:var(--fg); color:var(--bg); font-weight:600; }
  .act.prim:hover:not(:disabled) { opacity:.88; }

  hr.sep { border:0; border-top:1px solid var(--line); margin:15px 0 0; }
  .err { color:var(--err); font-size:11px; margin-top:9px; min-height:0; font-family:var(--mono); line-height:1.5; }

  /* colour / operation audit */
  .aud { margin-top:2px; }
  .aud .crow { display:flex; align-items:center; gap:8px; font-family:var(--mono); font-size:11px;
        color:var(--fg); padding:3px 0; font-variant-numeric:tabular-nums; }
  .aud .sw2 { width:13px; height:13px; border-radius:3px; border:1px solid var(--line); flex:none; }
  .aud .arw { color:var(--mut); }
  .aud .resv { color:var(--err); font-size:10px; }
  .aud .warn { color:var(--err); font-size:10.5px; font-family:var(--sans); line-height:1.45; margin:7px 0 0;
        display:flex; gap:6px; }
  .aud .warn::before { content:"!"; color:var(--acc); font-family:var(--mono); font-weight:700; }
  .aud .skip { color:var(--mut); font-size:10.5px; font-family:var(--sans); margin-top:7px; }
  .drop { display:flex; align-items:center; justify-content:center; gap:9px;
          border:1px dashed var(--line); border-radius:7px; padding:8px 12px; text-align:center;
          color:var(--mut); cursor:pointer; font-size:11.5px; font-family:var(--mono); line-height:1.35;
          background:var(--faint); transition:border-color .14s,background .14s,color .14s; }
  .drop:hover { border-color:var(--mut); color:var(--fg); }
  .drop.hot { border-color:var(--acc); color:var(--fg); background:color-mix(in srgb,var(--acc) 9%,var(--faint)); }
  .drop .ico { flex:none; width:15px; height:15px; transition:transform .16s; }
  .drop:hover .ico { transform:translateY(-2px); }
</style>
</head>
<body>
<header>
  <h1 class="brand">
    <span class="vec"><span class="shaft"></span><span class="head"></span></span>
    <span class="word"><b>epilog</b><span class="thin">·ue</span></span>
  </h1>
  <span class="chip">epilogue · preflight</span>
  <span class="grow"></span>
  <span class="chip">Epilog-safe SVG</span>
  <!--TOOLZ-NAV-->
</header>
<div class="wrap">
  <div class="panel">
    <div id="drop" class="drop"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V4"/><path d="M7 9l5-5 5 5"/><path d="M5 20h14"/></svg><span>Drop an SVG, or click to upload &nbsp;<small>from Inkscape / Affinity / …</small></span></div>
    <input id="file" type="file" accept=".svg,image/svg+xml" class="hide">

    <div class="grp"><h3>Scale &amp; units</h3>
      <label class="row"><span>width (mm)</span>
        <span class="tip" data-tip="Force the physical output width in millimetres (height keeps the aspect ratio). Leave blank to read the size declared in the file. Use this to fix the Inkscape/Affinity scaling bug where a file imports 25–33% too big.">i</span></label>
      <input type="number" id="width_mm" placeholder="from file" step="1" min="0">
      <label class="row"><span>units</span>
        <span class="tip" data-tip="Unit written in the output SVG header (width/height). The internal viewBox stays in millimetres either way, so the file always imports at true scale.">i</span></label>
      <select id="units"><option value="mm">mm</option><option value="cm">cm</option><option value="in">in</option></select>
      <label class="row"><span>source DPI</span>
        <span class="tip" data-tip="px→mm assumption for unitless / px lengths in the source. 96 = modern Inkscape, 90 = older Inkscape, 72 = Illustrator. Only matters when the file's size is in px.">i</span></label>
      <select id="dpi"><option value="96">96 · modern Inkscape</option><option value="90">90 · old Inkscape</option><option value="72">72 · Illustrator</option></select>
    </div>

    <hr class="sep">

    <div class="grp"><h3>Optimizations</h3>
      <div class="chk"><span class="sw"><input type="checkbox" id="hairline" checked><span class="tr"></span></span>
        <label for="hairline">hairline-normalize</label>
        <span class="tip" data-tip="Collapse every path to the laser cut invariant: fill:none + one thin hairline stroke. Nothing gets mistaken for a raster engrave. Turn OFF to keep the source stroke/fill/width (scaled to mm) — for files already styled correctly.">i</span></div>
      <div class="chk"><span class="sw"><input type="checkbox" id="snap_colors"><span class="tr"></span></span>
        <label for="snap_colors">snap colours</label>
        <span class="tip" data-tip="Round each operation's colour to the nearest clean primary (red/green/blue/…) so it matches an Epilog Color Mapping row EXACTLY. Dodges the driver's two traps: pure black is reserved for the General tab, and any unmatched colour is silently treated as black.">i</span></div>
      <p class="hint" id="snapNote"></p>

      <div id="hairOpts">
        <label class="row"><span>hairline width (mm)</span>
          <span class="tip" data-tip="Stroke width for cut lines. The laser ignores it, so keep it a hairline (≤0.1mm) — that's how the Epilog driver tells a vector cut from a raster engrave.">i</span></label>
        <input type="text" id="stroke_width" value="0.02">
        <label class="row"><span>cut colour</span>
          <span class="tip" data-tip="Stroke colour for the single cut op when hairline-normalizing (and not snapping). Black is fine for a one-operation cut file — the Epilog driver runs black off its General tab.">i</span></label>
        <input type="text" id="color" value="#000000">
      </div>
    </div>

    <hr class="sep">

    <div class="grp"><h3>Colour / operation audit</h3>
      <div class="aud" id="audit"><p class="hint">Upload an SVG to see its operation colours.</p></div>
    </div>

    <button id="dlBtn" class="act prim" disabled>Download Epilog-safe SVG</button>
    <div class="err" id="status"></div>
  </div>

  <div class="stage">
    <div class="seg hide" id="seg">
      <button id="segAfter" class="on">epilogued</button>
      <button id="segBefore">original</button>
    </div>
    <div class="paper hide" id="paper"><div id="out"></div></div>
    <div class="empty" id="empty">Upload an SVG to begin…</div>
    <div class="stat" id="stat"></div>
  </div>
</div>
<div id="tipbox"></div>
<script>
const $=id=>document.getElementById(id);
let token=null, original="", lastSvg="", view="after";
const status=m=>$('status').textContent=m||"";

// ---- tooltips: one shared fixed box, immune to panel clipping ----
const tipbox=$('tipbox');
document.querySelectorAll('.tip').forEach(el=>{
  el.addEventListener('mouseenter',()=>{
    tipbox.textContent=el.dataset.tip; tipbox.classList.add('show');
    const r=el.getBoundingClientRect();
    tipbox.style.left=Math.min(r.left-8, innerWidth-260)+'px';
    tipbox.style.top=(r.bottom+7)+'px';
  });
  el.addEventListener('mouseleave',()=>tipbox.classList.remove('show'));
});

// ---- upload (dropzone or picker) ----
async function upload(f){
  if(!f) return;
  status('reading…');
  const fd=new FormData(); fd.append('svg', f);
  const r=await (await fetch('/upload',{method:'POST',body:fd})).json();
  if(r.error){ status(r.error); return; }
  token=r.id; original=r.svg;
  $('empty').classList.add('hide'); $('paper').classList.remove('hide'); $('seg').classList.remove('hide');
  $('dlBtn').disabled=false; status('');
  render();
}
$('drop').onclick=()=>$('file').click();
$('file').onchange=e=>upload(e.target.files[0]);
['dragenter','dragover'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('hot');}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hot');}));
$('drop').addEventListener('drop',e=>{ if(e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });

// ---- params + live render (debounced) ----
function params(){
  return { id:token, width_mm:$('width_mm').value, units:$('units').value, dpi:$('dpi').value,
    hairline:$('hairline').checked, snap_colors:$('snap_colors').checked,
    stroke_width:$('stroke_width').value, color:$('color').value };
}
let timer=null;
function schedule(){ clearTimeout(timer); timer=setTimeout(render,140); }

async function render(){
  if(!token) return;
  $('hairOpts').classList.toggle('hide', !$('hairline').checked);
  $('snapNote').textContent = (!$('hairline').checked && $('snap_colors').checked)
    ? 'snap has no effect while hairline is off (source colours are kept).' : '';
  const r=await (await fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(params())})).json();
  if(r.error){ status(r.error); return; }
  status(''); lastSvg=r.svg;
  drawAudit(r.stats);
  const s=r.stats;
  $('stat').textContent=`${s.width_mm}×${s.height_mm}mm · ${s.paths} paths / ${s.points} pts`;
  if(view==='after') show(lastSvg, true); else show(original, false);
}

// inject an svg string and scale it to fit the stage (unit-agnostic: measure, then size)
function show(svgText, isAfter){
  const box=$('out'); box.innerHTML=svgText;
  box.classList.toggle('hair', isAfter);
  const el=box.querySelector('svg'); if(!el) return;
  el.style.width=el.style.height='';
  const nat=el.getBoundingClientRect();
  const st=$('paper').parentElement;
  const availW=st.clientWidth-72, availH=st.clientHeight-72;
  const k=Math.min(1, availW/(nat.width||300), availH/(nat.height||150));
  el.style.width=(nat.width*k)+'px'; el.style.height=(nat.height*k)+'px';
}

function drawAudit(s){
  const a=$('audit'); a.innerHTML='';
  (s.colors||[]).forEach(c=>{
    const row=document.createElement('div'); row.className='crow';
    const isBlack=c.out==='#000000';
    row.innerHTML=`<span class="sw2" style="background:${c.out}"></span>`
      +`<span>${c.count}×</span><span>${c.src}</span>`
      +(c.out!==c.src?`<span class="arw">→ ${c.out}</span>`:'')
      +(isBlack?`<span class="resv">reserved</span>`:'');
    a.appendChild(row);
  });
  (s.color_warnings||[]).forEach(w=>{
    const d=document.createElement('div'); d.className='warn'; d.append(document.createTextNode(w)); a.appendChild(d);
  });
  const sk=s.warnings||{}; const bits=[];
  if(sk.text) bits.push(`${sk.text} <text> (convert to paths)`);
  if(sk.image) bits.push(`${sk.image} raster <image>`);
  if(sk.use) bits.push(`${sk.use} unresolved <use>`);
  if(bits.length){ const d=document.createElement('div'); d.className='skip'; d.textContent='skipped: '+bits.join(', '); a.appendChild(d); }
  if(!(s.colors||[]).length && !bits.length) a.innerHTML='<p class="hint">no vector geometry found.</p>';
}

// ---- before/after ----
$('segAfter').onclick=()=>{ view='after'; $('segAfter').classList.add('on'); $('segBefore').classList.remove('on'); show(lastSvg,true); };
$('segBefore').onclick=()=>{ view='before'; $('segBefore').classList.add('on'); $('segAfter').classList.remove('on'); show(original,false); };

// ---- wire every control to live render ----
['width_mm','units','dpi','hairline','snap_colors','stroke_width','color']
  .forEach(id=>{ const el=$(id); el.addEventListener('input',schedule); el.addEventListener('change',schedule); });
addEventListener('resize',()=>{ if(token) show(view==='after'?lastSvg:original, view==='after'); });

// ---- download ----
$('dlBtn').onclick=async()=>{
  if(!token) return;
  status('building…');
  const resp=await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params())});
  if(!resp.ok){ status('download failed'); return; }
  const blob=await resp.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='epilog.svg'; a.click(); status('downloaded');
};
</script></body></html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5003"))     # 5001 server.py · 5002 segment_server.py
    print(f"epilogue_server on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)
