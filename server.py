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
import logging
import os.path
import uuid
from dataclasses import fields

from flask import Flask, jsonify, request, Response
from PIL import Image, UnidentifiedImageError

from linify import Params, image_to_svg

app = Flask(__name__)

# Quiet the per-request access log — it spams the CLI on every slider drag
# (each render is a POST). Warnings/errors still surface.
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# In-memory stores keyed by token. Local single-user tool, so plain dicts are
# fine — no persistence, cleared on restart. _NAMES keeps the uploaded file's
# stem so downloads can be named after the source image.
_IMAGES: dict[str, Image.Image] = {}
_NAMES: dict[str, str] = {}

# Explicit type map — can't infer from defaults (amp/mask_threshold default to
# None) and PEP 563 makes dataclass .type a string, so name the coercion here.
_BOOL_FIELDS = {"invert", "freq_mod", "mesh", "islands_only"}
_INT_FIELDS = {"samples", "resample", "levels", "cells_wide", "hatch_lines",
               "points", "tsp_improve"}
_STR_FIELDS = {"mode", "color", "units", "spacing_style", "fill_style",
               "smooth_mode", "contour_source"}
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
        return jsonify(error="no file received"), 400
    upload = request.files["image"]
    raw = upload.read()
    if not raw:
        return jsonify(error="the file is empty"), 400
    name = upload.filename or "file"
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force a full decode so truncated/corrupt data fails here
        img = img.convert("L")
    except (UnidentifiedImageError, OSError, ValueError):
        return jsonify(error=f"couldn't read '{name}' — not a valid image file"), 400
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
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><g fill='%23ff453a' stroke='%23ff453a' stroke-width='2.6' stroke-linecap='round'><circle cx='32' cy='32' r='8.5' stroke='none'/><line x1='32.00' y1='21.90' x2='32.00' y2='2.90'/><line x1='34.61' y1='22.24' x2='37.20' y2='12.58'/><line x1='37.05' y1='23.25' x2='45.55' y2='8.53'/><line x1='39.14' y1='24.86' x2='45.51' y2='18.49'/><line x1='40.75' y1='26.95' x2='57.20' y2='17.45'/><line x1='41.76' y1='29.39' x2='52.38' y2='26.54'/><line x1='42.10' y1='32.00' x2='58.10' y2='32.00'/><line x1='41.76' y1='34.61' x2='50.45' y2='36.94'/><line x1='40.75' y1='37.05' x2='57.20' y2='46.55'/><line x1='39.14' y1='39.14' x2='46.21' y2='46.21'/><line x1='37.05' y1='40.75' x2='45.55' y2='55.47'/><line x1='34.61' y1='41.76' x2='36.94' y2='50.45'/><line x1='32.00' y1='42.10' x2='32.00' y2='61.10'/><line x1='29.39' y1='41.76' x2='26.54' y2='52.38'/><line x1='26.95' y1='40.75' x2='18.95' y2='54.60'/><line x1='24.86' y1='39.14' x2='18.49' y2='45.51'/><line x1='23.25' y1='37.05' x2='6.80' y2='46.55'/><line x1='22.24' y1='34.61' x2='12.58' y2='37.20'/><line x1='21.90' y1='32.00' x2='4.90' y2='32.00'/><line x1='22.24' y1='29.39' x2='13.55' y2='27.06'/><line x1='23.25' y1='26.95' x2='6.80' y2='17.45'/><line x1='24.86' y1='24.86' x2='17.08' y2='17.08'/><line x1='26.95' y1='23.25' x2='18.95' y2='9.40'/><line x1='29.39' y1='22.24' x2='27.06' y2='13.55'/></g></svg>">
<style>
  :root {
    /* dark-first: this IS the primary design; the light block below is the override */
    --bg:#0a0b0d; --panel:#111318; --stage:#050506; --line:#23262d;
    --fg:#e7e9ed; --mut:#767c87; --faint:#171a20; --field:#0d0f13;
    --acc:#ff453a; --acc-fg:#ffffff; --err:#ff6b5e;      /* laser red accent */
    --warn:#f0a83a;                                      /* amber — caution affordances */
    --paper:#f7f7f4; --shadow:0 18px 60px rgba(0,0,0,.6);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg:#f1f1ef; --panel:#ffffff; --stage:#e6e6e3; --line:#e0e0dc;
      --fg:#17181b; --mut:#83868d; --faint:#f5f5f3; --field:#fbfbfa;
      --acc:#e5322a; --acc-fg:#ffffff; --err:#c0392b;
      --warn:#b26a00;
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

  /* (i) info dot — hover to reveal a tooltip (positioned by JS, see #tip) */
  .info { display:inline-flex; align-items:center; justify-content:center; flex:none;
          width:13px; height:13px; margin-left:5px; border:1px solid var(--line); border-radius:50%;
          color:var(--mut); font:italic 600 9px/1 var(--sans); cursor:help; vertical-align:middle;
          transition:color .12s,border-color .12s; }
  .info:hover { color:var(--acc); border-color:var(--acc); }
  .lbl { display:inline-flex; align-items:center; }   /* keep text + (i) together */
  .tip { position:fixed; z-index:50; max-width:240px; padding:7px 10px; border-radius:6px;
         background:var(--panel); color:var(--fg); border:1px solid var(--line);
         box-shadow:0 8px 30px rgba(0,0,0,.45); font:11px/1.45 var(--sans); pointer-events:none;
         opacity:0; transform:translateY(-3px); transition:opacity .1s,transform .1s; }
  .tip.show { opacity:1; transform:translateY(0); }

  /* ⚠ warning button — click opens the risk modal. Escalates amber→red with settings */
  .warn { display:inline-flex; align-items:center; justify-content:center; flex:none;
          gap:4px; margin-left:7px; padding:1px 7px 1px 5px; border-radius:11px;
          border:1px solid color-mix(in srgb,var(--warn) 55%,var(--line));
          background:color-mix(in srgb,var(--warn) 12%,transparent); color:var(--warn);
          font:600 9.5px/1 var(--mono); letter-spacing:.04em; text-transform:uppercase;
          cursor:pointer; vertical-align:middle; transition:background .12s,border-color .12s,box-shadow .12s; }
  .warn:hover { background:color-mix(in srgb,var(--warn) 22%,transparent);
                border-color:var(--warn); }
  .warn .tri { font-size:10px; }
  .warn.danger { --warn:var(--err); box-shadow:0 0 0 3px color-mix(in srgb,var(--err) 16%,transparent); }

  /* risk modal — dim backdrop + centered card (sized to never scroll) */
  .modal-bg { position:fixed; inset:0; z-index:100; display:none; align-items:center;
              justify-content:center; padding:24px; background:rgba(0,0,0,.55);
              backdrop-filter:blur(3px); }
  .modal-bg.show { display:flex; }
  .modal { max-width:440px; width:100%; background:var(--panel); border:1px solid var(--line);
           border-radius:12px; box-shadow:0 24px 70px rgba(0,0,0,.6); padding:20px 22px; }
  .modal h2 { margin:0 0 10px; font:600 14px/1.3 var(--sans); color:var(--fg);
              display:flex; align-items:center; gap:8px; }
  .modal h2 .tri { color:var(--warn); font-size:16px; }
  .modal .risk { display:flex; gap:8px; margin:0 0 8px; padding-left:9px;
                 border-left:2px solid var(--line); font:11.5px/1.45 var(--sans); }
  .modal .risk.hot { border-left-color:var(--warn); }
  .modal .risk b { flex:none; color:var(--fg); }
  .modal .risk span { color:var(--mut); }
  .modal .live { margin:11px 0 0; padding:8px 10px; border-radius:7px;
                 background:var(--faint); border:1px solid var(--line);
                 font:10.5px/1.5 var(--mono); color:var(--mut); }
  .modal .live b { color:var(--warn); }
  .modal .close { margin-top:13px; width:100%; padding:9px; border-radius:7px;
                  border:1px solid var(--line); background:var(--field); color:var(--fg);
                  font:600 12px var(--mono); cursor:pointer; transition:opacity .12s; }
  .modal .close:hover { opacity:.85; }

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

  /* danger-range field — red box + red slider fill, but still editable (non-blocking) */
  .num.danger-field, input[type=text].danger-field { border-color:var(--err);
        box-shadow:0 0 0 3px color-mix(in srgb,var(--err) 18%,transparent); }
  input[type=range].danger-field::-webkit-slider-runnable-track {
        background:linear-gradient(90deg,var(--err) 0 var(--p,0%),var(--line) var(--p,0%) 100%); }
  input[type=range].danger-field::-moz-range-progress { background:var(--err); }
  input[type=range].danger-field::-webkit-slider-thumb { box-shadow:0 0 0 2px var(--panel),0 0 0 4px color-mix(in srgb,var(--err) 40%,transparent); }

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

  /* upload dropzone — slim single-line bar with an upload glyph */
  .drop { display:flex; align-items:center; justify-content:center; gap:9px;
          border:1px dashed var(--line); border-radius:7px; padding:8px 12px;
          text-align:center; color:var(--mut); cursor:pointer; font-size:11.5px; font-family:var(--mono);
          line-height:1.35; background:var(--faint); transition:border-color .14s,background .14s,color .14s; }
  .drop:hover { border-color:var(--mut); color:var(--fg); }
  .drop.hot { border-color:var(--acc); color:var(--fg);
        background:color-mix(in srgb,var(--acc) 9%,var(--faint)); }
  .drop small { color:var(--mut); }
  .drop .ico { flex:none; width:15px; height:15px; transition:transform .16s; }
  .drop:hover .ico { transform:translateY(-2px); }   /* arrow lifts on hover */
  .drop.hot .ico { transform:translateY(-2px); color:var(--acc); }

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
    <div id="drop" class="drop"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V4"/><path d="M7 9l5-5 5 5"/><path d="M5 20h14"/></svg><span>Drop an image, or click to upload &nbsp;<small>PNG / JPG</small></span></div>
    <input id="file" type="file" accept="image/*" class="hide">

    <div class="grp"><h3>Mode
      <button type="button" id="wavyWarn" class="warn" title="Laser-cutter risks of short-wavelength wavy"><span class="tri">⚠</span>Laser risks</button></h3>
      <div class="modes" id="modes">
        <button data-m="wavy" class="on">wavy</button>
        <button data-m="spacing">spacing</button>
        <button data-m="contour">contour</button>
        <button data-m="filet">filet</button>
        <button data-m="flow">flow</button>
        <button data-m="tsp">tsp</button>
      </div>
    </div>

    <hr class="sep">

    <div class="grp"><h3>Output</h3>
      <label class="row"><span class="lbl">Units<span class="info" data-tip="Physical unit for the export header and every length field below. Only the labeling changes — the geometry imports at true scale either way.">i</span></span></label>
      <select id="units">
        <option value="mm">millimetres (mm)</option>
        <option value="in">inches (in)</option>
        <option value="cm">centimetres (cm)</option>
      </select>
      <label class="row"><span class="lbl">Width (<span id="u_width">mm</span>)<span class="info" data-tip="Physical output width. Height is derived automatically from the image's aspect ratio.">i</span></span> <input class="num" type="number" id="n_width"></label>
      <input type="range" id="width" min="20" max="1000" step="1" value="200">
      <label class="row"><span class="lbl">Color<span class="info" data-tip="Single stroke color for every line. Use red for a cut layer, black to engrave — width never encodes tone.">i</span></span></label>
      <input type="text" id="color" value="black">
      <div class="swatches">
        <button style="background:#000" data-c="black" title="black (engrave)"></button>
        <button style="background:#e23" data-c="red" title="red (cut)"></button>
        <button style="background:#26f" data-c="blue" title="blue"></button>
      </div>
      <div class="chk"><input type="checkbox" id="invert"><label for="invert">Invert tones</label><span class="info" data-tip="Swap the dark↔light tonal encoding (and flip what counts as background for masking).">i</span></div>
      <div class="chk"><input type="checkbox" id="mask_enabled"><label for="mask_enabled">Mask background</label><span class="info" data-tip="Draw nothing where brightness is above the threshold — erases a light background so only the subject is rendered.">i</span></div>
      <div id="mask_ctl" class="hide">
        <label class="row"><span class="lbl">Mask threshold<span class="info" data-tip="Brightness cutoff for masking (0–1). Higher keeps more of the image; lower erases more of the background.">i</span></span> <input class="num" type="number" id="n_mask_threshold"></label>
        <input type="range" id="mask_threshold" min="0.5" max="1" step="0.01" value="0.9">
      </div>
    </div>

    <hr class="sep">

    <!-- wavy -->
    <div class="grp mode-wavy"><h3>Wavy <span class="info" data-tip="Displaced horizontal scanlines: darkness modulates the wiggle amplitude (the face-in-lines look).">i</span></h3>
      <label class="row"><span class="lbl">Line spacing (<span class="uu">mm</span>)<span class="info" data-tip="Distance between scanline baselines — sets how many lines are drawn.">i</span></span> <input class="num" type="number" id="n_line_spacing"></label>
      <input type="range" id="line_spacing" min="0.5" max="8" step="0.05" value="2">
      <label class="row"><span class="lbl">Amplitude (<span class="uu">mm</span>)<span class="info" data-tip="Maximum wiggle size. Auto-clamped below half the line spacing so adjacent lines can never cross.">i</span></span> <input class="num" type="number" id="n_amp"></label>
      <input type="range" id="amp" min="0" max="4" step="0.02" value="1">
      <label class="row"><span class="lbl">Amplitude gamma<span class="info" data-tip="Amplitude response curve. Below 1 lifts midtones so mid-gray wiggles hard — more contrast in the darks. Untuned (1.0) looks flat.">i</span></span> <input class="num" type="number" id="n_amp_gamma"></label>
      <input type="range" id="amp_gamma" min="0.2" max="2" step="0.05" value="1">
      <label class="row"><span class="lbl">Phase jitter<span class="info" data-tip="Per-line phase offset (0–1). Decorrelates scanlines so bulges stop aligning into vertical banding — reads as woven ink.">i</span></span> <input class="num" type="number" id="n_phase_jitter"></label>
      <input type="range" id="phase_jitter" min="0" max="1" step="0.02" value="0">
      <label class="row"><span class="lbl">Wavelength (<span class="uu">mm</span>)<span class="info" data-tip="Carrier wavelength of the horizontal waves — the spacing of one full wiggle.">i</span></span> <input class="num" type="number" id="n_wavelength"></label>
      <input type="range" id="wavelength" min="1" max="30" step="0.25" value="8">
      <div class="chk"><input type="checkbox" id="freq_mod"><label for="freq_mod">Frequency modulation</label><span class="info" data-tip="Also raise the wave frequency (not just amplitude) in dark regions, so shadows get a denser, faster wiggle.">i</span></div>
      <label class="row"><span class="lbl">Frequency amount<span class="info" data-tip="Strength of frequency modulation (0–2). Only takes effect when Frequency modulation is enabled.">i</span></span> <input class="num" type="number" id="n_freq_amount"></label>
      <input type="range" id="freq_amount" min="0" max="2" step="0.05" value="1">
    </div>

    <!-- spacing -->
    <div class="grp mode-spacing hide"><h3>Spacing <span class="info" data-tip="Density lines: horizontal lines pack closer in dark regions and spread apart in light ones.">i</span></h3>
      <label class="row"><span class="lbl">Style<span class="info" data-tip="clean = crisp lines clipped to the subject (silhouette). density = per-column accumulation, recovers internal shading but looks dottier.">i</span></span></label>
      <select id="spacing_style">
        <option value="clean">clean — crisp lines, silhouette</option>
        <option value="density">density — internal shading (dottier)</option>
      </select>
      <label class="row"><span class="lbl">Minimum spacing / dark (<span class="uu">mm</span>)<span class="info" data-tip="Line spacing in the darkest regions — how tightly lines pack in the shadows.">i</span></span> <input class="num" type="number" id="n_min_spacing"></label>
      <input type="range" id="min_spacing" min="0.1" max="4" step="0.02" value="0.6">
      <label class="row"><span class="lbl">Maximum spacing / light (<span class="uu">mm</span>)<span class="info" data-tip="Line spacing in the lightest regions — how far lines spread in the highlights.">i</span></span> <input class="num" type="number" id="n_max_spacing"></label>
      <input type="range" id="max_spacing" min="0.5" max="12" step="0.05" value="4">
    </div>

    <!-- contour -->
    <div class="grp mode-contour hide"><h3>Contour <span class="info" data-tip="Topographic iso-lines: brightness is quantized into bands and each equal-brightness contour is traced.">i</span></h3>
      <label class="row"><span class="lbl">Levels<span class="info" data-tip="Number of brightness bands. More levels = more contour lines and finer tonal steps.">i</span></span> <input class="num" type="number" id="n_levels"></label>
      <input type="range" id="levels" min="2" max="40" step="1" value="8">
      <label class="row"><span class="lbl">Smoothing (sigma px)<span class="info" data-tip="Gaussian blur applied before tracing (sigma in px). Try 1–2 to smooth jagged contours.">i</span></span> <input class="num" type="number" id="n_smooth"></label>
      <input type="range" id="smooth" min="0" max="6" step="0.1" value="1.5">
      <label class="row"><span class="lbl">Minimum contour length (<span class="uu">mm</span>)<span class="info" data-tip="Drop any contour shorter than this — removes speckle and tiny stray loops.">i</span></span> <input class="num" type="number" id="n_min_contour_len"></label>
      <input type="range" id="min_contour_len" min="0" max="30" step="0.25" value="2">
      <label class="row"><span class="lbl">Trace<span class="info" data-tip="'Tone' traces iso-brightness bands (classic). 'Edge' traces the gradient magnitude, so contours hug every boundary — an edge-forensic look with the same speckle character.">i</span></span>
        <select id="contour_source"><option value="tone">tone (brightness)</option><option value="edge">edge (gradient)</option></select></label>
      <label class="row"><span class="lbl">Smoothing kernel<span class="info" data-tip="'Gaussian' blurs across edges (softens everything). 'Bilateral' is edge-preserving: it flattens flat regions to kill garbage contours while keeping boundaries razor-sharp and small high-contrast specks intact.">i</span></span>
        <select id="smooth_mode"><option value="gaussian">gaussian</option><option value="bilateral">bilateral (edge-preserving)</option></select></label>
      <label class="row"><span class="lbl">Bilateral edge hardness<span class="info" data-tip="Tonal sigma for the bilateral kernel (0–1). Smaller = harder edges / more preserved detail; larger acts more like a plain blur. Only used when the smoothing kernel is bilateral.">i</span></span> <input class="num" type="number" id="n_bilateral_color"></label>
      <input type="range" id="bilateral_color" min="0.02" max="0.5" step="0.01" value="0.1">
      <div class="chk"><input type="checkbox" id="islands_only"><label for="islands_only">Islands only</label><span class="info" data-tip="Keep only closed loops and drop the long open contours — a pure speckle-field of the small artifacts. Pair with a Maximum contour length to also discard big loops.">i</span></div>
      <label class="row"><span class="lbl">Minimum contour area (mm²)<span class="info" data-tip="Drop closed loops enclosing less than this area. Unlike Minimum length (a perimeter), this filters by blob size — a long thin sliver survives, a tiny compact loop doesn't. 0 = off.">i</span></span> <input class="num" type="number" id="n_min_contour_area"></label>
      <input type="range" id="min_contour_area" min="0" max="50" step="0.5" value="0">
      <label class="row"><span class="lbl">Maximum contour length (<span class="uu">mm</span>)<span class="info" data-tip="Drop any contour longer than this — discards the big backbone contours and keeps the small artifacts. 0 = off.">i</span></span> <input class="num" type="number" id="n_max_contour_len"></label>
      <input type="range" id="max_contour_len" min="0" max="200" step="1" value="0">
    </div>

    <!-- filet -->
    <div class="grp mode-filet hide"><h3>Filet <span class="info" data-tip="Filet-crochet grid: the image is quantized to a grid of filled vs. open cells. Dark cells become filled squares on an open mesh — a chart you could crochet from.">i</span></h3>
      <label class="row"><span class="lbl">Cells wide<span class="info" data-tip="Number of grid columns. Rows are derived automatically to keep the cells square. More cells = finer detail, smaller squares.">i</span></span> <input class="num" type="number" id="n_cells_wide"></label>
      <input type="range" id="cells_wide" min="8" max="160" step="1" value="60">
      <label class="row"><span class="lbl">Fill threshold<span class="info" data-tip="How dark a cell must be to become filled (0–1). Lower = more cells fill in (heavier); higher = only the darkest cells fill.">i</span></span> <input class="num" type="number" id="n_fill_threshold"></label>
      <input type="range" id="fill_threshold" min="0.05" max="0.95" step="0.01" value="0.5">
      <label class="row"><span class="lbl">Fill mark<span class="info" data-tip="How a filled cell is drawn (the laser ignores stroke width, so 'filled' must be geometry). X = classic chart cross; cross = plus; hatch = parallel diagonals (most solid / tonal).">i</span></span></label>
      <select id="fill_style">
        <option value="x">X — classic filet chart mark</option>
        <option value="cross">cross — plus (+) mark</option>
        <option value="hatch">hatch — parallel diagonals (solid)</option>
      </select>
      <label class="row mode-filet-hatch hide"><span class="lbl">Hatch lines / cell<span class="info" data-tip="Number of parallel diagonals packed into each filled cell when the fill mark is 'hatch'. More = more solid.">i</span></span> <input class="num" type="number" id="n_hatch_lines"></label>
      <input type="range" id="hatch_lines" class="mode-filet-hatch hide" min="1" max="10" step="1" value="3">
      <div class="chk"><input type="checkbox" id="mesh" checked><label for="mesh">Draw mesh grid</label><span class="info" data-tip="Draw the full grid lattice (every cell border) as long continuous hairlines — the open filet mesh. Off: only filled cells are drawn, each with its own outline, floating on blank ground.">i</span></div>
    </div>

    <!-- flow -->
    <div class="grp mode-flow hide"><h3>Flow <span class="info" data-tip="Edge-tangent hatching: short streamlines flow along the form (around eyes, jaw, folds). Stroke density encodes tone — dense in shadow, sparse in light.">i</span></h3>
      <label class="row"><span class="lbl">Seed spacing (<span class="uu">mm</span>)<span class="info" data-tip="Grid pitch between candidate strokes. Smaller = denser hatching (heavier files); larger = sparser.">i</span></span> <input class="num" type="number" id="n_flow_spacing"></label>
      <input type="range" id="flow_spacing" min="0.4" max="5" step="0.05" value="1.4">
      <label class="row"><span class="lbl">Stroke length (<span class="uu">mm</span>)<span class="info" data-tip="Arc length of each streamline. Longer strokes read as flowing lines; shorter as a stipple-like weave.">i</span></span> <input class="num" type="number" id="n_flow_len"></label>
      <input type="range" id="flow_len" min="1" max="30" step="0.5" value="7">
      <label class="row"><span class="lbl">Field coherence (sigma px)<span class="info" data-tip="Structure-tensor blur that averages the flow direction. Higher = smoother, more coherent flow; lower = follows fine detail (and noise).">i</span></span> <input class="num" type="number" id="n_flow_smooth"></label>
      <input type="range" id="flow_smooth" min="1" max="20" step="0.5" value="6">
      <label class="row"><span class="lbl">Integration step (<span class="uu">mm</span>)<span class="info" data-tip="Distance marched per step along a streamline. Smaller = smoother strokes and more points; larger = coarser, lighter files.">i</span></span> <input class="num" type="number" id="n_flow_step"></label>
      <input type="range" id="flow_step" min="0.1" max="1.5" step="0.05" value="0.4">
      <label class="row"><span class="lbl">Density gamma<span class="info" data-tip="Seed-density response curve. Below 1 lifts midtones so mid-gray fills with strokes — more coverage and contrast.">i</span></span> <input class="num" type="number" id="n_flow_gamma"></label>
      <input type="range" id="flow_gamma" min="0.2" max="2" step="0.05" value="1">
    </div>

    <!-- tsp -->
    <div class="grp mode-tsp hide"><h3>TSP <span class="info" data-tip="Single continuous line: the image is stippled into dots (dense in shadow) and every dot is joined into one unbroken traveling-salesman path — the whole picture in a single stroke.">i</span></h3>
      <label class="row"><span class="lbl">Dot count<span class="info" data-tip="Number of stipple points — vertices in the single tour. More dots = more detail and a longer, denser line (slower to compute).">i</span></span> <input class="num" type="number" id="n_points"></label>
      <input type="range" id="points" min="500" max="12000" step="100" value="4000">
      <label class="row"><span class="lbl">Density gamma<span class="info" data-tip="Darkness weighting for dot placement. Below 1 lifts midtones so mid-gray gets more dots — fuller shading.">i</span></span> <input class="num" type="number" id="n_point_gamma"></label>
      <input type="range" id="point_gamma" min="0.2" max="2" step="0.05" value="1">
      <label class="row"><span class="lbl">Untangle passes<span class="info" data-tip="Neighbor-limited 2-opt passes that remove long crossings from the tour. 0 = raw nearest-neighbor (fast, more crossings); 2–3 = cleaner line (slower).">i</span></span> <input class="num" type="number" id="n_tsp_improve"></label>
      <input type="range" id="tsp_improve" min="0" max="5" step="1" value="2">
    </div>

    <hr class="sep">

    <!-- advanced (rows tagged mode-* apply to a subset; untagged = all modes) -->
    <div class="grp">
      <h3 class="adv-h" id="adv_h">Advanced</h3>
      <div id="adv_body" class="hide">
        <p class="hint">Fine-grained knobs — type exact values into any box above too.</p>
        <label class="row mode-wavy mode-spacing"><span class="lbl">Samples per line<span class="info" data-tip="Points sampled along each line. More = smoother curves and larger files. Wavy & spacing only.">i</span></span> <input class="num" type="number" id="n_samples"></label>
        <input type="range" id="samples" class="mode-wavy mode-spacing" min="100" max="4000" step="10" value="800">
        <label class="row"><span class="lbl">Resample edge (px)<span class="info" data-tip="Working image resolution (longest edge, px). Higher recovers more detail but renders slower.">i</span></span> <input class="num" type="number" id="n_resample"></label>
        <input type="range" id="resample" min="200" max="2000" step="10" value="900">
        <label class="row mode-wavy mode-spacing mode-contour mode-flow mode-tsp"><span class="lbl">Decimation (<span class="uu">mm</span>)<span class="info" data-tip="Collinear-point removal tolerance. Smaller keeps more points (heavier files); larger simplifies. Not used by filet (grid is already minimal).">i</span></span> <input class="num" type="number" id="n_decimate"></label>
        <input type="range" id="decimate" class="mode-wavy mode-spacing mode-contour mode-flow mode-tsp" min="0" max="0.5" step="0.005" value="0.03">
        <label class="row"><span class="lbl">Stroke width (<span class="uu">mm</span>)<span class="info" data-tip="Hairline width. Tonally irrelevant — the laser ignores stroke width — it only affects on-screen visibility.">i</span></span> <input class="num" type="number" id="n_stroke_width"></label>
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
<div id="tip" class="tip"></div>

<!-- short-wavelength wavy → laser-cutter risk modal (Epilog Mini) -->
<div id="warnBg" class="modal-bg">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="warnTitle">
    <h2 id="warnTitle"><span class="tri">⚠</span>Short-wavelength wavy — Epilog Mini</h2>
    <div class="risk" id="risk-wear"><b>Motion wear</b><span>Rapid reversals jerk-load the belts &amp; bearings. One job won't misalign it (firmware caps jerk, it re-homes), but sustained sub-2&nbsp;mm λ stretches belts and drifts registration over time.</span></div>
    <div class="risk" id="risk-burn"><b>Over-burn</b><span>The head dwells at each turnaround, so packed cusps char, widen kerf and can flare up on wood/acrylic — the most immediate hazard.</span></div>
    <div class="risk" id="risk-reso"><b>Resonance</b><span>Reversal rate near the gantry's resonance causes chatter and blurred waves.</span></div>
    <div class="risk" id="risk-res"><b>Wasted detail</b><span>Under ~0.5–1&nbsp;mm, kerf + slop smear the wiggle into a scorched band.</span></div>

    <p class="live" id="warnLive"></p>

    <button type="button" class="close" id="warnClose">Got it</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let imgId = null, mode = "wavy", unit = "mm", timer = null;

const MM_PER = { mm:1, cm:10, in:25.4 };
const WIDTH_CFG = { mm:{min:20,max:1000,step:1}, cm:{min:2,max:100,step:0.1}, in:{min:1,max:40,step:0.1} };

// length fields whose values track the selected unit. Base ranges are stored in
// mm and rescaled on unit change; on send they convert back to mm (collect()).
// Everything not listed here is unit-agnostic (px counts, ratios, 0..1).
const MM_FIELDS = {
  line_spacing:{min:0.5,max:8,step:0.05}, amp:{min:0,max:4,step:0.02},
  wavelength:{min:1,max:30,step:0.25}, min_spacing:{min:0.1,max:4,step:0.02},
  max_spacing:{min:0.5,max:12,step:0.05}, min_contour_len:{min:0,max:30,step:0.25},
  max_contour_len:{min:0,max:200,step:1},
  flow_spacing:{min:0.4,max:5,step:0.05}, flow_len:{min:1,max:30,step:0.5},
  flow_step:{min:0.1,max:1.5,step:0.05},
  decimate:{min:0,max:0.5,step:0.005}, stroke_width:{min:0.001,max:0.5,step:0.001},
};
const unitRound = v => +v.toFixed(unit==="mm" ? 3 : 4);

// rescale one length field into the current unit, holding its physical size constant
function scaleField(id, prevUnit){
  const base=MM_FIELDS[id], r=$(id), n=$("n_"+id), f=MM_PER[unit];
  const mm=(+r.value)*MM_PER[prevUnit];
  const mn=unitRound(base.min/f), mx=unitRound(base.max/f), st=base.step/f;
  const v=Math.min(mx, Math.max(mn, unitRound(mm/f)));
  [r,n].forEach(el=>{ el.min=mn; el.max=mx; el.step=st; el.value=v; });
  fillRange(id);
}

// every control with a range + paired number input (id === Params field, except
// "width", whose value is in the selected unit and converts to width_mm on send)
const RANGES = ["width","mask_threshold","line_spacing","amp","amp_gamma","phase_jitter",
  "wavelength","freq_amount","min_spacing","max_spacing","levels","smooth","min_contour_len",
  "bilateral_color","min_contour_area","max_contour_len",
  "cells_wide","fill_threshold","hatch_lines",
  "flow_spacing","flow_len","flow_smooth","flow_step","flow_gamma",
  "points","point_gamma","tsp_improve",
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
  const g = id => +$(id).value;                  // unit-agnostic (px, ratios, counts)
  const gmm = id => +$(id).value * MM_PER[unit];  // length fields -> mm for the backend
  return {
    id: imgId, mode, units: unit,
    width_mm: gmm("width"), color: $("color").value,
    invert: $("invert").checked,
    mask_enabled: $("mask_enabled").checked, mask_threshold: g("mask_threshold"),
    line_spacing: gmm("line_spacing"), amp: gmm("amp"), wavelength: gmm("wavelength"),
    amp_gamma: g("amp_gamma"), phase_jitter: g("phase_jitter"),
    freq_mod: $("freq_mod").checked, freq_amount: g("freq_amount"),
    min_spacing: gmm("min_spacing"), max_spacing: gmm("max_spacing"),
    spacing_style: $("spacing_style").value,
    levels: g("levels"), smooth: g("smooth"), min_contour_len: gmm("min_contour_len"),
    smooth_mode: $("smooth_mode").value, contour_source: $("contour_source").value,
    bilateral_color: g("bilateral_color"), islands_only: $("islands_only").checked,
    min_contour_area: g("min_contour_area"), max_contour_len: gmm("max_contour_len"),
    cells_wide: g("cells_wide"), fill_threshold: g("fill_threshold"),
    fill_style: $("fill_style").value, hatch_lines: g("hatch_lines"), mesh: $("mesh").checked,
    flow_spacing: gmm("flow_spacing"), flow_len: gmm("flow_len"), flow_step: gmm("flow_step"),
    flow_smooth: g("flow_smooth"), flow_gamma: g("flow_gamma"),
    points: g("points"), point_gamma: g("point_gamma"), tsp_improve: g("tsp_improve"),
    samples: g("samples"), resample: g("resample"), decimate: gmm("decimate"),
    stroke_width: gmm("stroke_width"),
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
  document.querySelectorAll(".mode-wavy,.mode-spacing,.mode-contour,.mode-filet,.mode-flow,.mode-tsp")
    .forEach(el=>el.classList.add("hide"));
  document.querySelectorAll(".mode-"+mode).forEach(el=>el.classList.remove("hide"));
  if(mode==="filet") syncFiletHatch();   // sub-controls depend on fill style
  updateWarn();
  render();
});

// filet: hatch-lines control only matters when the fill mark is 'hatch'
function syncFiletHatch(){
  const on = mode==="filet" && $("fill_style").value==="hatch";
  document.querySelectorAll(".mode-filet-hatch").forEach(el=>el.classList.toggle("hide", !on));
}

// units: preserve every physical size, rescale all length controls into the new unit
$("units").addEventListener("change", ()=>{
  const prev = unit;
  unit = $("units").value;
  // width has its own (wider) per-unit range table; the rest share MM_FIELDS
  const wmm = (+$("width").value) * MM_PER[prev];
  const cfg = WIDTH_CFG[unit];
  const wv = Math.min(cfg.max, Math.max(cfg.min, +(wmm/MM_PER[unit]).toFixed(3)));
  ["width","n_width"].forEach(id=>{ const el=$(id); el.min=cfg.min; el.max=cfg.max; el.step=cfg.step; el.value=wv; });
  fillRange("width");
  Object.keys(MM_FIELDS).forEach(id=>scaleField(id, prev));
  document.querySelectorAll(".uu, #u_width").forEach(el=>el.textContent=unit);
  render();
});

initPairs();
["color"].forEach(id=>$(id).addEventListener("input", debounced));
["invert","freq_mod","mesh","islands_only"].forEach(id=>$(id).addEventListener("change", render));
$("spacing_style").addEventListener("change", render);
$("contour_source").addEventListener("change", render);
$("smooth_mode").addEventListener("change", render);
$("fill_style").addEventListener("change", ()=>{ syncFiletHatch(); render(); });
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
const DROP_HTML = drop.innerHTML;          // initial prompt — restored if an upload fails
drop.addEventListener("click", ()=>file.click());
// clear the input's value after reading so re-picking the same file still fires "change"
file.addEventListener("change", ()=>{ const f=file.files[0]; file.value=""; if(f) upload(f); });
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("hot");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("hot");}));
drop.addEventListener("drop", e=>{ const f=e.dataTransfer.files[0]; if(f) upload(f); });

async function upload(f){
  if(f.type && !f.type.startsWith("image/")){
    $("err").textContent = `“${f.name}” isn't an image file`; return;
  }
  $("err").textContent = "";
  drop.textContent = "uploading…";
  const fd = new FormData(); fd.append("image", f);
  try{
    const r = await fetch("/upload",{method:"POST",body:fd});
    const j = await r.json().catch(()=>({error:`server error (${r.status})`}));
    if(!r.ok || j.error){
      $("err").textContent = j.error || `upload failed (${r.status})`;
      drop.innerHTML = DROP_HTML;
      return;
    }
    imgId = j.id;
    drop.innerHTML = `${f.name} · ${j.w}×${j.h}px<br><small>downloads as ${j.name}_${mode}.svg · click to replace</small>`;
    render();
  }catch(e){
    $("err").textContent = "upload failed — is the server still running?";
    drop.innerHTML = DROP_HTML;
  }
}

$("dl").addEventListener("click", ()=>{
  if(!imgId){ $("err").textContent="upload an image first"; return; }
  const p = collect();
  const q = new URLSearchParams();
  Object.entries(p).forEach(([k,v])=>q.set(k, typeof v==="boolean" ? (v?"1":"0") : v));
  window.location = "/download?"+q.toString();
});

// tooltips: one floating box, positioned under the hovered (i) and clamped to the viewport
const tip = $("tip");
document.addEventListener("mouseover", e=>{
  const i = e.target.closest(".info"); if(!i) return;
  tip.textContent = i.dataset.tip || "";
  tip.classList.add("show");
  const r = i.getBoundingClientRect(), pad = 8;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = Math.max(pad, Math.min(r.left + r.width/2 - tw/2, innerWidth - tw - pad));
  let y = r.bottom + 7;
  if(y + th + pad > innerHeight) y = r.top - th - 7;   // flip above if it would overflow
  tip.style.left = x + "px"; tip.style.top = y + "px";
});
document.addEventListener("mouseout", e=>{
  if(e.target.closest(".info")) tip.classList.remove("show");
});

// short-wavelength wavy → laser-cutter risk warning
const warnBtn = $("wavyWarn"), warnBg = $("warnBg");
function warnState(){
  const wl_mm = (+$("wavelength").value) * MM_PER[unit];            // set wavelength, in mm
  // frequency modulation shortens the effective wavelength in dark regions (d_row→1):
  // inv_wl = (1/wl)*(1+freq_amount)  ⇒  eff wavelength = wl/(1+freq_amount)
  const eff = $("freq_mod").checked ? wl_mm/(1 + (+$("freq_amount").value)) : wl_mm;
  return { wl_mm, eff, danger: eff <= 2, caution: eff <= 4 };
}
function updateWarn(){
  const s = warnState();
  // wavelength only matters in wavy mode — don't cry danger from a stale value elsewhere
  const danger = s.danger && mode === "wavy";
  warnBtn.classList.toggle("danger", danger);
  // paint the wavelength field red (non-blocking — user can still cut)
  ["wavelength","n_wavelength"].forEach(id=>$(id).classList.toggle("danger-field", danger));
  ["risk-wear","risk-burn","risk-res"].forEach(id=>$(id).classList.toggle("hot", s.caution && mode==="wavy"));
  const pitch = (s.eff/2).toFixed(2);
  const fm = $("freq_mod").checked ? ` (effective ${s.eff.toFixed(2)} mm with freq-mod)` : "";
  const lead = s.danger ? "<b>DANGER — </b>" : s.caution ? "<b>Caution — </b>" : "";
  $("warnLive").innerHTML =
    `${lead}wavelength ${s.wl_mm.toFixed(2)} mm${fm} → head reverses every `
    + `<b>${pitch} mm</b>. ` + (s.danger
        ? "Slow the job, lower power, and watch it — never run unattended."
        : s.caution ? "Getting short — keep λ ≥ ~4 mm for clean, low-stress cuts."
        : "Comfortable range for the Epilog Mini.");
}
warnBtn.addEventListener("click", ()=>{ updateWarn(); warnBg.classList.add("show"); });
function closeWarn(){ warnBg.classList.remove("show"); }
$("warnClose").addEventListener("click", closeWarn);
warnBg.addEventListener("click", e=>{ if(e.target===warnBg) closeWarn(); });
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeWarn(); });
["wavelength","n_wavelength","freq_amount","n_freq_amount"].forEach(id=>
  $(id).addEventListener("input", updateWarn));
$("freq_mod").addEventListener("change", updateWarn);
$("units").addEventListener("change", updateWarn);
updateWarn();
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
