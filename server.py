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
_STR_FIELDS = {"mode", "color", "units", "spacing_style"}
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
<title>laser-toolz</title>
<style>
  :root {
    /* dark-first: this IS the primary design; the light block below is the override */
    --bg:#0a0b0d; --panel:#111318; --stage:#050506; --line:#23262d;
    --fg:#e7e9ed; --mut:#767c87; --faint:#171a20; --field:#0d0f13;
    --acc:#ff453a; --acc-fg:#ffffff; --err:#ff6b5e;      /* laser red accent */
    --paper:#f7f7f4; --shadow:0 18px 60px rgba(0,0,0,.6);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f1f1ef; --panel:#ffffff; --stage:#e6e6e3; --line:#e0e0dc;
      --fg:#17181b; --mut:#83868d; --faint:#f5f5f3; --field:#fbfbfa;
      --acc:#e5322a; --acc-fg:#ffffff; --err:#c0392b;
      --paper:#ffffff; --shadow:0 12px 44px rgba(20,20,25,.14);
    }
  }
  * { box-sizing:border-box; }
  .hide { display:none !important; }
  html,body { height:100%; }
  body { margin:0; color:var(--fg); background:var(--bg); display:flex; flex-direction:column;
         font:13px/1.5 var(--sans); -webkit-font-smoothing:antialiased; }

  /* header + vector wordmark (red arrow = the "vector") */
  header { display:flex; align-items:center; padding:13px 22px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  .brand { margin:0; display:inline-flex; flex-direction:column; align-items:stretch; gap:3px; }
  .vec { display:flex; align-items:center; height:8px; padding:0 1px; }
  .vec .shaft { flex:1; height:1.5px; background:var(--acc); border-radius:1px; }
  .vec .head { width:0; height:0; margin-left:-1px; border-left:6px solid var(--acc);
               border-top:4px solid transparent; border-bottom:4px solid transparent; }
  .word { font-family:var(--mono); font-size:16px; font-weight:600; letter-spacing:-.01em; line-height:1; }
  .word .thin { color:var(--mut); font-weight:400; }

  .wrap { display:flex; flex:1; min-height:0; }
  .panel { width:451px; flex:none; border-right:1px solid var(--line); background:var(--panel);
           overflow-y:auto; padding:16px 18px 26px; }
  .panel::-webkit-scrollbar { width:9px; }
  .panel::-webkit-scrollbar-thumb { background:var(--line); border-radius:9px;
           border:3px solid var(--panel); }

  .stage { position:relative; flex:1; min-width:0; display:flex; align-items:center; justify-content:center;
           background:var(--stage); overflow:auto; padding:32px; }
  .paper { background:var(--paper); border-radius:2px; box-shadow:var(--shadow); }
  .paper svg { display:block; max-width:72vw; max-height:82vh; height:auto; width:auto; }
  /* Hairlines are ~0.02mm — invisible on screen. Force a visible width for PREVIEW
     ONLY; the downloaded file keeps the true hairline. (viewBox is always mm, so
     this stays correct regardless of the selected output unit.) */
  .paper svg .ink { stroke-width:0.32 !important; }
  .empty { color:var(--mut); padding:48px; font-size:12px; text-align:center; font-family:var(--mono); }

  /* groups — mono micro-headers with a trailing hairline rule */
  .grp { margin-top:15px; }
  .grp:first-of-type { margin-top:2px; }
  .grp h3 { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.14em;
            color:var(--mut); margin:0 0 9px; font-weight:600;
            display:flex; align-items:center; gap:8px; }
  .grp h3::after { content:""; flex:1; height:1px; background:var(--line); }

  /* compact rows: mono label + inline value on one line, slider tight below */
  label.row { display:flex; align-items:baseline; justify-content:space-between; gap:10px;
              margin:9px 0 3px; color:var(--fg); font-size:11.5px; font-family:var(--mono);
              letter-spacing:-.01em; }
  label.row:first-child { margin-top:0; }
  .hint { color:var(--mut); font-size:10.5px; margin:2px 0 0; font-family:var(--sans); }

  /* fields */
  input[type=text], select { width:100%; background:var(--field); color:var(--fg);
        border:1px solid var(--line); border-radius:6px; padding:6px 9px; font-family:var(--mono);
        font-size:12px; outline:none; transition:border-color .12s,box-shadow .12s; }
  select { appearance:none; cursor:pointer;
        background-image:linear-gradient(45deg,transparent 50%,var(--mut) 50%),linear-gradient(135deg,var(--mut) 50%,transparent 50%);
        background-position:calc(100% - 15px) 13px,calc(100% - 10px) 13px;
        background-size:5px 5px,5px 5px; background-repeat:no-repeat; padding-right:28px; }
  input[type=text]:focus, select:focus, .num:focus {
        border-color:var(--acc); box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 20%,transparent); }
  .num { width:72px; background:var(--field); color:var(--fg); border:1px solid var(--line);
         border-radius:5px; padding:2px 7px; font-variant-numeric:tabular-nums; font-size:11.5px;
         text-align:right; outline:none; -moz-appearance:textfield; font-family:var(--mono);
         transition:border-color .12s,box-shadow .12s; }
  .num::-webkit-outer-spin-button,.num::-webkit-inner-spin-button { -webkit-appearance:none; margin:0; }

  /* range sliders — thin track, red fill (--p set from JS; Firefox via -moz-range-progress) */
  input[type=range] { -webkit-appearance:none; appearance:none; width:100%; height:16px;
        background:transparent; cursor:pointer; margin:0; }
  input[type=range]::-webkit-slider-runnable-track { height:3px; border-radius:3px;
        background:linear-gradient(90deg,var(--acc) 0 var(--p,0%),var(--line) var(--p,0%) 100%); }
  input[type=range]::-moz-range-track { height:3px; border-radius:3px; background:var(--line); }
  input[type=range]::-moz-range-progress { height:3px; border-radius:3px; background:var(--acc); }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:12px; height:12px;
        margin-top:-4.5px; border-radius:50%; background:var(--fg); border:0;
        box-shadow:0 0 0 2px var(--panel); transition:transform .1s; }
  input[type=range]::-moz-range-thumb { width:11px; height:11px; border-radius:50%;
        background:var(--fg); border:0; box-shadow:0 0 0 2px var(--panel); }
  input[type=range]:hover::-webkit-slider-thumb { transform:scale(1.15); }
  input[type=range]:focus-visible::-webkit-slider-thumb {
        box-shadow:0 0 0 2px var(--panel),0 0 0 5px color-mix(in srgb,var(--acc) 30%,transparent); }

  /* segmented mode control — active pill ringed in red */
  .modes { display:flex; gap:2px; padding:3px; background:var(--faint);
           border:1px solid var(--line); border-radius:8px; }
  .modes button { flex:1; padding:6px 0; background:transparent; color:var(--mut);
        border:0; border-radius:5px; cursor:pointer; font-family:var(--mono); font-size:11.5px;
        letter-spacing:.02em; transition:background .14s,color .14s,box-shadow .14s; }
  .modes button:hover { color:var(--fg); }
  .modes button.on { background:var(--field); color:var(--fg);
        box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--acc) 45%,transparent); }

  /* checkboxes */
  .chk { display:flex; align-items:center; gap:9px; margin:9px 0; font-size:11.5px; font-family:var(--mono); }
  .chk input { width:15px; height:15px; accent-color:var(--acc); cursor:pointer; margin:0; }
  .chk label { cursor:pointer; }

  /* swatches (real stroke colors — functional, kept) */
  .swatches { display:flex; gap:7px; margin-top:8px; }
  .swatches button { width:20px; height:20px; border-radius:50%; border:1px solid var(--line);
        cursor:pointer; padding:0; transition:transform .1s,box-shadow .1s; }
  .swatches button:hover { transform:scale(1.15);
        box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 18%,transparent); }

  /* advanced disclosure (h3 base supplies the trailing rule + gap) */
  .adv-h { cursor:pointer; user-select:none; }
  .adv-h::before { content:""; flex:none; width:0; height:0; border-left:5px solid var(--mut);
        border-top:4px solid transparent; border-bottom:4px solid transparent; transition:transform .15s; }
  .adv-h.open::before { transform:rotate(90deg); }

  /* upload dropzone — slim single-line bar */
  .drop { border:1px dashed var(--line); border-radius:7px; padding:7px 12px;
          text-align:center; color:var(--mut); cursor:pointer; font-size:11.5px; font-family:var(--mono);
          line-height:1.35; background:var(--faint); transition:border-color .14s,background .14s,color .14s; }
  .drop:hover { border-color:var(--mut); }
  .drop.hot { border-color:var(--acc); color:var(--fg);
        background:color-mix(in srgb,var(--acc) 9%,var(--faint)); }
  .drop small { color:var(--mut); }

  /* action bar — high-contrast primary (red stays reserved as an accent) */
  .bar { margin-top:16px; }
  .bar button { width:100%; padding:10px; border-radius:7px; border:1px solid var(--fg);
        background:var(--fg); color:var(--bg); cursor:pointer; font-family:var(--mono);
        font-size:12px; font-weight:600; letter-spacing:.02em; transition:opacity .12s,transform .05s; }
  .bar button:hover { opacity:.88; }
  .bar button:active { transform:translateY(1px); }

  hr.sep { border:0; border-top:1px solid var(--line); margin:15px 0 0; }

  .err { color:var(--err); font-size:11px; margin-top:9px; min-height:14px; font-family:var(--mono); }

  /* stat pill floating over the stage */
  .stat { position:absolute; left:18px; bottom:14px; color:var(--mut); font-size:11px;
          font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:.02em;
          background:color-mix(in srgb,var(--panel) 78%,transparent); backdrop-filter:blur(8px);
          border:1px solid var(--line); border-radius:6px; padding:5px 10px; }
  .stat:empty { display:none; }
</style>
</head>
<body>
<header>
  <h1 class="brand">
    <span class="vec"><span class="shaft"></span><span class="head"></span></span>
    <span class="word"><b>laser</b><span class="thin">·toolz</span></span>
  </h1>
</header>
<div class="wrap">
  <div class="panel">
    <div id="drop" class="drop">Drop an image, or click to upload&nbsp; <small>PNG / JPG</small></div>
    <input id="file" type="file" accept="image/*" class="hide">

    <div class="grp"><h3>Mode</h3>
      <div class="modes" id="modes">
        <button data-m="wavy" class="on">wavy</button>
        <button data-m="spacing">spacing</button>
        <button data-m="contour">contour</button>
      </div>
    </div>

    <hr class="sep">

    <div class="grp"><h3>Output</h3>
      <label class="row">Units</label>
      <select id="units">
        <option value="mm">millimetres (mm)</option>
        <option value="in">inches (in)</option>
        <option value="cm">centimetres (cm)</option>
      </select>
      <label class="row"><span>Width (<span id="u_width">mm</span>)</span> <input class="num" type="number" id="n_width"></label>
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
        <label class="row"><span>Mask threshold</span> <input class="num" type="number" id="n_mask_threshold"></label>
        <input type="range" id="mask_threshold" min="0.5" max="1" step="0.01" value="0.9">
      </div>
    </div>

    <hr class="sep">

    <!-- wavy -->
    <div class="grp mode-wavy"><h3>Wavy</h3>
      <label class="row"><span>Line spacing (mm)</span> <input class="num" type="number" id="n_line_spacing"></label>
      <input type="range" id="line_spacing" min="0.5" max="8" step="0.05" value="2">
      <label class="row"><span>Amplitude (mm)</span> <input class="num" type="number" id="n_amp"></label>
      <input type="range" id="amp" min="0" max="4" step="0.02" value="1">
      <label class="row"><span>Amplitude gamma</span> <input class="num" type="number" id="n_amp_gamma"></label>
      <input type="range" id="amp_gamma" min="0.2" max="2" step="0.05" value="1">
      <p class="hint">&lt;1 lifts midtones — more contrast in the darks</p>
      <label class="row"><span>Phase jitter</span> <input class="num" type="number" id="n_phase_jitter"></label>
      <input type="range" id="phase_jitter" min="0" max="1" step="0.02" value="0">
      <p class="hint">decorrelates lines — breaks vertical banding</p>
      <label class="row"><span>Wavelength (mm)</span> <input class="num" type="number" id="n_wavelength"></label>
      <input type="range" id="wavelength" min="1" max="30" step="0.25" value="8">
      <div class="chk"><input type="checkbox" id="freq_mod"><label for="freq_mod">Frequency modulation</label></div>
      <label class="row"><span>Frequency amount</span> <input class="num" type="number" id="n_freq_amount"></label>
      <input type="range" id="freq_amount" min="0" max="2" step="0.05" value="1">
    </div>

    <!-- spacing -->
    <div class="grp mode-spacing hide"><h3>Spacing</h3>
      <label class="row">Style</label>
      <select id="spacing_style">
        <option value="clean">clean — crisp lines, silhouette</option>
        <option value="density">density — internal shading (dottier)</option>
      </select>
      <label class="row"><span>Minimum spacing / dark (mm)</span> <input class="num" type="number" id="n_min_spacing"></label>
      <input type="range" id="min_spacing" min="0.1" max="4" step="0.02" value="0.6">
      <label class="row"><span>Maximum spacing / light (mm)</span> <input class="num" type="number" id="n_max_spacing"></label>
      <input type="range" id="max_spacing" min="0.5" max="12" step="0.05" value="4">
    </div>

    <!-- contour -->
    <div class="grp mode-contour hide"><h3>Contour</h3>
      <label class="row"><span>Levels</span> <input class="num" type="number" id="n_levels"></label>
      <input type="range" id="levels" min="2" max="40" step="1" value="8">
      <label class="row"><span>Smoothing (sigma px)</span> <input class="num" type="number" id="n_smooth"></label>
      <input type="range" id="smooth" min="0" max="6" step="0.1" value="1.5">
      <label class="row"><span>Minimum contour length (mm)</span> <input class="num" type="number" id="n_min_contour_len"></label>
      <input type="range" id="min_contour_len" min="0" max="30" step="0.25" value="2">
    </div>

    <hr class="sep">

    <!-- advanced (all modes) -->
    <div class="grp">
      <h3 class="adv-h" id="adv_h">Advanced</h3>
      <div id="adv_body" class="hide">
        <p class="hint">Fine-grained knobs — type exact values into any box above too.</p>
        <label class="row"><span>Samples per line</span> <input class="num" type="number" id="n_samples"></label>
        <input type="range" id="samples" min="100" max="4000" step="10" value="800">
        <label class="row"><span>Resample edge (px)</span> <input class="num" type="number" id="n_resample"></label>
        <input type="range" id="resample" min="200" max="2000" step="10" value="900">
        <label class="row"><span>Decimation (mm)</span> <input class="num" type="number" id="n_decimate"></label>
        <input type="range" id="decimate" min="0" max="0.5" step="0.005" value="0.03">
        <label class="row"><span>Stroke width (mm)</span> <input class="num" type="number" id="n_stroke_width"></label>
        <input type="range" id="stroke_width" min="0.001" max="0.5" step="0.001" value="0.02">
      </div>
    </div>

    <div class="bar">
      <button id="dl" class="primary">Download SVG</button>
    </div>
    <div class="err" id="err"></div>
  </div>

  <div class="stage">
    <div id="paper" class="paper"><div class="empty">Upload an image to begin…</div></div>
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
const RANGES = ["width","mask_threshold","line_spacing","amp","amp_gamma","phase_jitter",
  "wavelength","freq_amount","min_spacing","max_spacing","levels","smooth","min_contour_len",
  "samples","resample","decimate","stroke_width"];

// paint the red fill on a range track (webkit reads --p; Firefox uses -moz-range-progress)
function fillRange(id){
  const r=$(id); if(!r) return;
  const p = (r.max==r.min) ? 0 : (r.value-r.min)/(r.max-r.min)*100;
  r.style.setProperty("--p", p+"%");
}

// mirror each range's min/max/step/value onto its number twin, then keep them synced
function initPairs(){
  RANGES.forEach(id=>{
    const r=$(id), n=$("n_"+id);
    n.min=r.min; n.max=r.max; n.step=r.step; n.value=r.value;
    fillRange(id);
    r.addEventListener("input", ()=>{ n.value=r.value; fillRange(id); debounced(); });
    n.addEventListener("input", ()=>{ if(n.value!=="") r.value=n.value; fillRange(id); debounced(); });
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
    amp_gamma: g("amp_gamma"), phase_jitter: g("phase_jitter"),
    freq_mod: $("freq_mod").checked, freq_amount: g("freq_amount"),
    min_spacing: g("min_spacing"), max_spacing: g("max_spacing"),
    spacing_style: $("spacing_style").value,
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
  fillRange("width");
  $("u_width").textContent = unit;
  render();
});

initPairs();
["color"].forEach(id=>$(id).addEventListener("input", debounced));
["invert","freq_mod"].forEach(id=>$(id).addEventListener("change", render));
$("spacing_style").addEventListener("change", render);
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
    print(f"laser-toolz web UI -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
