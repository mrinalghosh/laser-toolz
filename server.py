#!/usr/bin/env python3
"""
server.py — local web UI for linify.

Upload an image, pick a mode, drag sliders, watch the hairline SVG update live,
then download the laser-ready file. Reuses linify.image_to_svg unchanged, so the
browser preview and the CLI produce byte-identical SVGs (same Params → same output).

Run:
    pip install flask
    python server.py           # -> http://127.0.0.1:5000
"""

from __future__ import annotations

import io
import re
import os.path
import uuid
from dataclasses import fields

from flask import Flask, jsonify, request, Response
from PIL import Image

from linify import Params, image_to_svg

app = Flask(__name__)

# In-memory stores keyed by token. Local single-user tool, so plain dicts are
# fine — no persistence, cleared on restart. _NAMES keeps the uploaded file's
# stem so downloads can be named after the source image.
_IMAGES: dict[str, Image.Image] = {}
_NAMES: dict[str, str] = {}

# Explicit type map — can't infer from defaults (amp/mask_threshold default to
# None) and PEP 563 makes dataclass .type a string, so name the coercion here.
_BOOL_FIELDS = {"invert", "freq_mod"}
_INT_FIELDS = {"samples", "resample", "levels"}
_STR_FIELDS = {"mode", "color", "units"}
_FLOAT_FIELDS = {f.name for f in fields(Params)} - _BOOL_FIELDS - _INT_FIELDS - _STR_FIELDS


def _safe_stem(filename: str) -> str:
    """Filesystem-safe stem of an uploaded filename (no path, no odd chars)."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem or "linify"


def _params_from_json(data: dict) -> Params:
    """Coerce a JSON payload of overrides into a Params (defaults fill the rest)."""
    p = Params()
    for name in {f.name for f in fields(Params)}:
        if name not in data or data[name] is None or data[name] == "":
            continue
        val = data[name]
        if name in _BOOL_FIELDS:
            val = val if isinstance(val, bool) else str(val) in ("1", "true", "True")
        elif name in _INT_FIELDS:
            val = int(float(val))
        elif name in _STR_FIELDS:
            val = str(val)
        else:  # float (incl. None-default fields like amp / mask_threshold)
            val = float(val)
        setattr(p, name, val)
    # mask toggle: only apply threshold when explicitly enabled
    if not data.get("mask_enabled"):
        p.mask_threshold = None
    return p


@app.post("/upload")
def upload():
    if "image" not in request.files:
        return jsonify(error="no image"), 400
    upload = request.files["image"]
    img = Image.open(io.BytesIO(upload.read())).convert("L")
    token = uuid.uuid4().hex
    _IMAGES[token] = img
    _NAMES[token] = _safe_stem(upload.filename)
    # keep the stores from growing unbounded across a long session
    if len(_IMAGES) > 24:
        for k in list(_IMAGES)[:-24]:
            _IMAGES.pop(k, None)
            _NAMES.pop(k, None)
    w, h = img.size
    return jsonify(id=token, w=w, h=h, name=_NAMES[token])


@app.post("/render")
def render():
    data = request.get_json(force=True)
    img = _IMAGES.get(data.get("id"))
    if img is None:
        return jsonify(error="unknown or expired image id; re-upload"), 404
    p = _params_from_json(data)
    try:
        svg, stats = image_to_svg(img, p)
    except Exception as exc:  # surface bad param combos to the UI
        return jsonify(error=str(exc)), 400
    return jsonify(svg=svg, stats=stats)


@app.get("/download")
def download():
    """Same render, delivered as a file attachment."""
    data = {k: (v[0] if isinstance(v, list) else v) for k, v in request.args.items()}
    img = _IMAGES.get(data.get("id"))
    if img is None:
        return "unknown image id", 404
    data["mask_enabled"] = data.get("mask_enabled") in ("1", "true", "True")
    for b in ("invert", "freq_mod"):
        data[b] = data.get(b) in ("1", "true", "True")
    p = _params_from_json(data)
    svg, _ = image_to_svg(img, p)
    stem = _NAMES.get(data.get("id"), "linify")
    return Response(
        svg,
        mimetype="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{stem}_{p.mode}.svg"'},
    )


@app.get("/")
def index():
    return Response(_PAGE, mimetype="text/html")


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>linify — image → laser SVG</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e7eaf0;
          --mut:#8a93a6; --acc:#5aa9ff; --paper:#f7f7f4; }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.45 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:12px; }
  header h1 { font-size:16px; margin:0; letter-spacing:.02em; }
  header .sub { color:var(--mut); font-size:12px; }
  .wrap { display:grid; grid-template-columns:340px 1fr; height:calc(100vh - 51px); }
  .panel { border-right:1px solid var(--line); background:var(--panel); overflow-y:auto; padding:16px; }
  .stage { position:relative; display:flex; align-items:center; justify-content:center;
           background:#0b0d11; overflow:auto; padding:24px; }
  .paper { background:var(--paper); box-shadow:0 8px 40px rgba(0,0,0,.5); }
  .paper svg { display:block; max-width:78vw; max-height:82vh; height:auto; width:auto; }
  /* Hairlines are ~0.02mm — invisible on screen. Force a visible width for PREVIEW
     ONLY; the downloaded file keeps the true hairline. (viewBox is always mm, so
     this stays correct regardless of the selected output unit.) */
  .paper svg .ink { stroke-width:0.32 !important; }
  label.row { display:block; margin:12px 0 4px; color:var(--mut); font-size:12px; }
  input[type=range] { width:100%; accent-color:var(--acc); }
  input[type=text], select { width:100%; background:#0e1116; color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
  .num { width:80px; float:right; background:#0e1116; color:var(--fg); border:1px solid var(--line);
         border-radius:5px; padding:2px 6px; font-variant-numeric:tabular-nums; font-size:12px;
         text-align:right; -moz-appearance:textfield; }
  .modes { display:flex; gap:6px; margin:6px 0 4px; }
  .modes button { flex:1; padding:8px 0; background:#0e1116; color:var(--fg);
        border:1px solid var(--line); border-radius:6px; cursor:pointer; font-size:13px; }
  .modes button.on { background:var(--acc); color:#04122a; border-color:var(--acc); font-weight:600; }
  .chk { display:flex; align-items:center; gap:8px; margin:10px 0; color:var(--fg); }
  .grp { border-top:1px solid var(--line); margin-top:14px; padding-top:6px; }
  .grp h3 { font-size:11px; text-transform:uppercase; letter-spacing:.08em;
            color:var(--mut); margin:6px 0 2px; }
  .adv-h { cursor:pointer; user-select:none; }
  .adv-h::before { content:"\25B8  "; }
  .adv-h.open::before { content:"\25BE  "; }
  .hint { color:var(--mut); font-size:11px; margin:2px 0 0; }
  .hide { display:none; }
  .drop { border:1.5px dashed var(--line); border-radius:8px; padding:18px; text-align:center;
          color:var(--mut); cursor:pointer; }
  .drop.hot { border-color:var(--acc); color:var(--fg); }
  .bar { display:flex; gap:8px; margin-top:14px; }
  .bar button { flex:1; padding:10px; border-radius:7px; border:1px solid var(--line);
                background:#0e1116; color:var(--fg); cursor:pointer; }
  .bar button.primary { background:var(--acc); color:#04122a; border-color:var(--acc); font-weight:600; }
  .stat { position:absolute; left:16px; bottom:12px; color:var(--mut); font-size:12px;
          font-variant-numeric:tabular-nums; }
  .err { color:#ff8080; font-size:12px; margin-top:8px; min-height:14px; }
  .swatches { display:flex; gap:6px; margin-top:6px; }
  .swatches button { width:24px; height:24px; border-radius:5px; border:1px solid var(--line); cursor:pointer; }
</style>
</head>
<body>
<header>
  <h1>linify</h1>
  <span class="sub">image → laser-ready hairline SVG · tone by geometry, never by width</span>
</header>
<div class="wrap">
  <div class="panel">
    <div id="drop" class="drop">Drop an image here, or click to choose<br><small>PNG / JPG</small></div>
    <input id="file" type="file" accept="image/*" class="hide">

    <div class="grp"><h3>Mode</h3>
      <div class="modes" id="modes">
        <button data-m="wavy" class="on">wavy</button>
        <button data-m="spacing">spacing</button>
        <button data-m="contour">contour</button>
      </div>
    </div>

    <div class="grp"><h3>Output</h3>
      <label class="row">Units</label>
      <select id="units">
        <option value="mm">millimetres (mm)</option>
        <option value="in">inches (in)</option>
        <option value="cm">centimetres (cm)</option>
      </select>
      <label class="row">Width (<span id="u_width">mm</span>) <input class="num" type="number" id="n_width"></label>
      <input type="range" id="width" min="20" max="1000" step="1" value="200">
      <label class="row">Color</label>
      <input type="text" id="color" value="black">
      <div class="swatches">
        <button style="background:#000" data-c="black" title="black (engrave)"></button>
        <button style="background:#e23" data-c="red" title="red (cut)"></button>
        <button style="background:#26f" data-c="blue" title="blue"></button>
      </div>
      <div class="chk"><input type="checkbox" id="invert"><label for="invert">Invert tones</label></div>
      <div class="chk"><input type="checkbox" id="mask_enabled"><label for="mask_enabled">Mask background</label></div>
      <div id="mask_ctl" class="hide">
        <label class="row">Mask threshold <input class="num" type="number" id="n_mask_threshold"></label>
        <input type="range" id="mask_threshold" min="0.5" max="1" step="0.01" value="0.9">
      </div>
    </div>

    <!-- wavy -->
    <div class="grp mode-wavy"><h3>Wavy</h3>
      <label class="row">Line spacing (mm) <input class="num" type="number" id="n_line_spacing"></label>
      <input type="range" id="line_spacing" min="0.5" max="8" step="0.05" value="2">
      <label class="row">Amplitude (mm) <input class="num" type="number" id="n_amp"></label>
      <input type="range" id="amp" min="0" max="4" step="0.02" value="1">
      <label class="row">Wavelength (mm) <input class="num" type="number" id="n_wavelength"></label>
      <input type="range" id="wavelength" min="1" max="30" step="0.25" value="8">
      <div class="chk"><input type="checkbox" id="freq_mod"><label for="freq_mod">Frequency modulation</label></div>
      <label class="row">Freq amount <input class="num" type="number" id="n_freq_amount"></label>
      <input type="range" id="freq_amount" min="0" max="2" step="0.05" value="1">
    </div>

    <!-- spacing -->
    <div class="grp mode-spacing hide"><h3>Spacing</h3>
      <label class="row">Min spacing / dark (mm) <input class="num" type="number" id="n_min_spacing"></label>
      <input type="range" id="min_spacing" min="0.1" max="4" step="0.02" value="0.6">
      <label class="row">Max spacing / light (mm) <input class="num" type="number" id="n_max_spacing"></label>
      <input type="range" id="max_spacing" min="0.5" max="12" step="0.05" value="4">
    </div>

    <!-- contour -->
    <div class="grp mode-contour hide"><h3>Contour</h3>
      <label class="row">Levels <input class="num" type="number" id="n_levels"></label>
      <input type="range" id="levels" min="2" max="40" step="1" value="8">
      <label class="row">Smooth (sigma px) <input class="num" type="number" id="n_smooth"></label>
      <input type="range" id="smooth" min="0" max="6" step="0.1" value="1.5">
      <label class="row">Min contour len (mm) <input class="num" type="number" id="n_min_contour_len"></label>
      <input type="range" id="min_contour_len" min="0" max="30" step="0.25" value="2">
    </div>

    <!-- advanced (all modes) -->
    <div class="grp">
      <h3 class="adv-h" id="adv_h">Advanced</h3>
      <div id="adv_body" class="hide">
        <p class="hint">Fine-grained knobs — type exact values into any box above too.</p>
        <label class="row">Samples / line <input class="num" type="number" id="n_samples"></label>
        <input type="range" id="samples" min="100" max="4000" step="10" value="800">
        <label class="row">Resample edge (px) <input class="num" type="number" id="n_resample"></label>
        <input type="range" id="resample" min="200" max="2000" step="10" value="900">
        <label class="row">Decimate (mm) <input class="num" type="number" id="n_decimate"></label>
        <input type="range" id="decimate" min="0" max="0.5" step="0.005" value="0.03">
        <label class="row">Stroke width (mm) <input class="num" type="number" id="n_stroke_width"></label>
        <input type="range" id="stroke_width" min="0.001" max="0.5" step="0.001" value="0.02">
      </div>
    </div>

    <div class="bar">
      <button id="dl" class="primary">Download SVG</button>
    </div>
    <div class="err" id="err"></div>
  </div>

  <div class="stage">
    <div id="paper" class="paper"><div style="color:#556;padding:40px">Upload an image to begin…</div></div>
    <div class="stat" id="stat"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let imgId = null, mode = "wavy", unit = "mm", timer = null;

const MM_PER = { mm:1, cm:10, in:25.4 };
const WIDTH_CFG = { mm:{min:20,max:1000,step:1}, cm:{min:2,max:100,step:0.1}, in:{min:1,max:40,step:0.1} };

// every control with a range + paired number input (id === Params field, except
// "width", whose value is in the selected unit and converts to width_mm on send)
const RANGES = ["width","mask_threshold","line_spacing","amp","wavelength","freq_amount",
  "min_spacing","max_spacing","levels","smooth","min_contour_len",
  "samples","resample","decimate","stroke_width"];

// mirror each range's min/max/step/value onto its number twin, then keep them synced
function initPairs(){
  RANGES.forEach(id=>{
    const r=$(id), n=$("n_"+id);
    n.min=r.min; n.max=r.max; n.step=r.step; n.value=r.value;
    r.addEventListener("input", ()=>{ n.value=r.value; debounced(); });
    n.addEventListener("input", ()=>{ if(n.value!=="") r.value=n.value; debounced(); });
  });
}

function collect(){
  const g = id => +$(id).value;
  return {
    id: imgId, mode, units: unit,
    width_mm: g("width") * MM_PER[unit], color: $("color").value,
    invert: $("invert").checked,
    mask_enabled: $("mask_enabled").checked, mask_threshold: g("mask_threshold"),
    line_spacing: g("line_spacing"), amp: g("amp"), wavelength: g("wavelength"),
    freq_mod: $("freq_mod").checked, freq_amount: g("freq_amount"),
    min_spacing: g("min_spacing"), max_spacing: g("max_spacing"),
    levels: g("levels"), smooth: g("smooth"), min_contour_len: g("min_contour_len"),
    samples: g("samples"), resample: g("resample"), decimate: g("decimate"),
    stroke_width: g("stroke_width"),
  };
}

async function render(){
  if(!imgId) return;
  $("stat").textContent = "rendering…";
  try{
    const r = await fetch("/render",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(collect())});
    const j = await r.json();
    if(j.error){ $("err").textContent = j.error; return; }
    $("err").textContent = "";
    $("paper").innerHTML = j.svg;
    const s = j.stats;
    const w = (s.width_disp ?? s.width_mm/MM_PER[unit]).toFixed(2);
    const h = (s.height_disp ?? s.height_mm/MM_PER[unit]).toFixed(2);
    $("stat").textContent = `${s.mode} · ${w}×${h} ${s.units||unit} · ${s.paths} paths · ${s.points} pts`;
  }catch(e){ $("err").textContent = String(e); }
}
function debounced(){ clearTimeout(timer); timer = setTimeout(render, 140); }

// mode switching
$("modes").addEventListener("click", e=>{
  const b = e.target.closest("button"); if(!b) return;
  mode = b.dataset.m;
  [...$("modes").children].forEach(x=>x.classList.toggle("on", x===b));
  document.querySelectorAll(".mode-wavy,.mode-spacing,.mode-contour")
    .forEach(el=>el.classList.add("hide"));
  document.querySelectorAll(".mode-"+mode).forEach(el=>el.classList.remove("hide"));
  render();
});

// units: preserve the physical size, rescale the width control into the new unit
$("units").addEventListener("change", ()=>{
  const mm = (+$("width").value) * MM_PER[unit];   // hold physical width constant
  unit = $("units").value;
  const cfg = WIDTH_CFG[unit];
  ["width","n_width"].forEach(id=>{ const el=$(id); el.min=cfg.min; el.max=cfg.max; el.step=cfg.step; });
  let v = mm / MM_PER[unit];
  v = Math.min(cfg.max, Math.max(cfg.min, +v.toFixed(3)));
  $("width").value = v; $("n_width").value = v;
  $("u_width").textContent = unit;
  render();
});

initPairs();
["color"].forEach(id=>$(id).addEventListener("input", debounced));
["invert","freq_mod"].forEach(id=>$(id).addEventListener("change", render));
$("mask_enabled").addEventListener("change", ()=>{
  $("mask_ctl").classList.toggle("hide", !$("mask_enabled").checked);
  render();
});
$("adv_h").addEventListener("click", ()=>{
  $("adv_h").classList.toggle("open");
  $("adv_body").classList.toggle("hide");
});
document.querySelectorAll(".swatches button").forEach(b=>
  b.addEventListener("click", ()=>{ $("color").value = b.dataset.c; render(); }));

// upload
const drop = $("drop"), file = $("file");
drop.addEventListener("click", ()=>file.click());
file.addEventListener("change", ()=>{ if(file.files[0]) upload(file.files[0]); });
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("hot");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("hot");}));
drop.addEventListener("drop", e=>{ const f=e.dataTransfer.files[0]; if(f) upload(f); });

async function upload(f){
  const fd = new FormData(); fd.append("image", f);
  drop.textContent = "uploading…";
  const r = await fetch("/upload",{method:"POST",body:fd});
  const j = await r.json();
  if(j.error){ $("err").textContent=j.error; return; }
  imgId = j.id;
  drop.innerHTML = `${f.name} · ${j.w}×${j.h}px<br><small>downloads as ${j.name}_${mode}.svg · click to replace</small>`;
  render();
}

$("dl").addEventListener("click", ()=>{
  if(!imgId){ $("err").textContent="upload an image first"; return; }
  const p = collect();
  const q = new URLSearchParams();
  Object.entries(p).forEach(([k,v])=>q.set(k, typeof v==="boolean" ? (v?"1":"0") : v));
  window.location = "/download?"+q.toString();
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    # Default to 5001: on macOS, port 5000 is taken by ControlCenter (AirPlay
    # Receiver), which returns 403 and looks like a crashed server. Override
    # with PORT=xxxx python server.py if needed.
    port = int(os.environ.get("PORT", "5001"))
    print(f"linify web UI -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
