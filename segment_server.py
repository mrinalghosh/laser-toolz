#!/usr/bin/env python3
"""
segment_server.py — interactive click-to-pick segmentation UI for segment.py.

A thin single-user Flask wrapper (same spirit as server.py) around MobileSAM's
*prompted* predictor. You upload an image, click objects to pick them, and each
picked mask becomes a labelled region in the downloaded SVG — reusing segment.py's
tracing and serialization so CLI and UI produce the same Inkscape-ready output.

Why a separate server from server.py: that one wraps linify's hairline modes;
this wraps a stateful SAM predictor with cached per-image embeddings, and unlike
segment.py's automatic generator, the prompted predictor runs fine on Apple MPS.

  python segment_server.py            # http://127.0.0.1:5002
  PORT=8080 python segment_server.py

Interaction:
  * left-click  = positive point (include this object)
  * shift-click = negative point (exclude — carve the mask back)
  * Add region  = freeze the current mask as a region
  * Download    = write all frozen regions to one segmentation SVG
"""

from __future__ import annotations

import io
import os
import secrets

import numpy as np
from flask import Flask, jsonify, request, send_file
from PIL import Image

from toolz_nav import nav_html
from linify import safe_stem
from segment import (
    SegParams, load_rgb, mask_to_rings, region_colors, regions_to_svg,
    _DEFAULT_CHECKPOINT, _pick_device,
)

app = Flask(__name__)

# Single-user, in-memory, cleared on restart. Each session token holds the
# working-resolution RGB array and the list of frozen (accepted) masks.
_SESS: dict[str, dict] = {}
_NAMES: dict[str, str] = {}

# One shared predictor (single-user). set_image is expensive (runs the image
# encoder), so we remember which token it currently holds and only recompute on
# a switch. Loaded lazily on first upload.
_PREDICTOR = None
_DEVICE = None
_CUR_TOKEN = None
_RESAMPLE = 1024


def _get_predictor():
    global _PREDICTOR, _DEVICE
    if _PREDICTOR is not None:
        return _PREDICTOR
    try:
        import warnings
        with warnings.catch_warnings():                    # quiet MobileSAM's import
            warnings.filterwarnings(                       # banner: timm.models.layers/
                "ignore", category=FutureWarning, module=r"timm\..*")  # .registry are
            warnings.filterwarnings(                       # deprecated, and it re-registers
                "ignore", message="Overwriting .* in registry", category=UserWarning)
            from mobile_sam import sam_model_registry, SamPredictor
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError(
            "segment_server needs mobile_sam:\n"
            "  pip install git+https://github.com/ChaoningZhang/MobileSAM.git"
        ) from exc
    ckpt = os.environ.get("MOBILE_SAM_CHECKPOINT", _DEFAULT_CHECKPOINT)
    if not os.path.exists(ckpt):
        raise RuntimeError(f"MobileSAM checkpoint not found at {ckpt!r}")
    # The prompted predictor casts point prompts to float32, so MPS is fine here
    # (unlike segment.py's automatic generator). Auto-pick mps when available.
    import torch
    dev = "cpu"
    if torch.cuda.is_available():
        dev = "cuda"
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        dev = "mps"
    sam = sam_model_registry["vit_t"](checkpoint=ckpt)
    sam.to(dev)
    sam.eval()
    _PREDICTOR, _DEVICE = SamPredictor(sam), dev
    return _PREDICTOR


def _ensure_image(token: str):
    """Point the shared predictor at `token`'s image, recomputing only on switch."""
    global _CUR_TOKEN
    sess = _SESS.get(token)
    if sess is None:
        return None
    pred = _get_predictor()
    if _CUR_TOKEN != token:
        pred.set_image(sess["rgb"])
        _CUR_TOKEN = token
    return pred


def _predict_mask(token: str, points):
    """Run the predictor for `points` (list of [x, y, label]); return best mask."""
    pred = _ensure_image(token)
    if pred is None or not points:
        return None
    coords = np.array([[p[0], p[1]] for p in points], dtype=np.float32)
    labels = np.array([int(p[2]) for p in points], dtype=np.int64)
    masks, scores, _ = pred.predict(
        point_coords=coords, point_labels=labels, multimask_output=True)
    return masks[int(np.argmax(scores))].astype(bool)


def _mask_polygons(mask: np.ndarray):
    """Contour polygons of `mask` in image px, for the client's live overlay."""
    import cv2
    m = (mask.astype(np.uint8)) * 255
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        if len(c) < 3:
            continue
        c = cv2.approxPolyDP(c, 1.5, True).reshape(-1, 2)
        out.append(c.tolist())
    return out


@app.post("/upload")
def upload():
    f = request.files.get("image")
    if f is None:
        return jsonify(error="no image"), 400
    img = Image.open(io.BytesIO(f.read()))
    orig_w, orig_h = img.size                              # ORIGINAL px (pre-resample)
    rgb, aspect = load_rgb(img, _RESAMPLE)
    token = secrets.token_urlsafe(8)
    h, w = rgb.shape[:2]
    _SESS[token] = {"rgb": rgb, "aspect": aspect, "orig": (orig_w, orig_h), "accepted": []}
    _NAMES[token] = safe_stem(f.filename, "segment")
    if len(_SESS) > 8:                                      # cap memory (embeddings are large)
        for k in list(_SESS)[:-8]:
            _SESS.pop(k, None)
            _NAMES.pop(k, None)
    return jsonify(id=token, w=w, h=h, orig_w=orig_w, orig_h=orig_h,
                   name=_NAMES[token], device=_get_predictor() and _DEVICE)


@app.get("/image/<token>")
def image(token):
    """Serve the working-resolution image so client click coords match mask px."""
    sess = _SESS.get(token)
    if sess is None:
        return jsonify(error="unknown token"), 404
    buf = io.BytesIO()
    Image.fromarray(sess["rgb"]).save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.post("/pick")
def pick():
    data = request.get_json(force=True)
    mask = _predict_mask(data.get("id"), data.get("points", []))
    if mask is None:
        return jsonify(polygons=[], area=0)
    _SESS[data["id"]]["current"] = mask                    # remember for /add
    return jsonify(polygons=_mask_polygons(mask), area=int(mask.sum()))


@app.post("/add")
def add():
    data = request.get_json(force=True)
    sess = _SESS.get(data.get("id"))
    if sess is None or sess.get("current") is None:
        return jsonify(error="no current mask"), 400
    sess["accepted"].append(sess["current"])
    sess["current"] = None
    return jsonify(count=len(sess["accepted"]))


@app.post("/undo")
def undo():
    sess = _SESS.get((request.get_json(force=True)).get("id"))
    if sess and sess["accepted"]:
        sess["accepted"].pop()
    return jsonify(count=len(sess["accepted"]) if sess else 0)


@app.post("/reset")
def reset():
    sess = _SESS.get((request.get_json(force=True)).get("id"))
    if sess:
        sess["accepted"] = []
        sess["current"] = None
    return jsonify(count=0)


@app.post("/download")
def download():
    data = request.get_json(force=True)
    sess = _SESS.get(data.get("id"))
    if sess is None or not sess["accepted"]:
        return jsonify(error="no regions picked"), 400

    p = SegParams(
        width_mm=float(data.get("width_mm", 200.0)),
        color=data.get("color", "label"),
        opacity=float(data.get("opacity", 1.0)),
        stroke=data.get("stroke", "none"),
        layers=bool(data.get("layers", False)),
        simplify=float(data.get("simplify", 0.15)),
    )
    rgb = sess["rgb"]
    img_h, img_w = rgb.shape[:2]
    width_mm = p.width_mm
    height_mm = width_mm * sess["aspect"]

    # Wrap accepted boolean masks as SAM-style dicts so region_colors / stats reuse.
    masks = [{"segmentation": m, "area": int(m.sum()), "predicted_iou": 1.0}
             for m in sess["accepted"]]
    colors = region_colors(rgb, masks, p)

    regions = []
    for i, (m, col) in enumerate(zip(masks, colors), 1):
        rings = mask_to_rings(m["segmentation"], img_w, img_h, width_mm, height_mm, p)
        if not rings:
            continue
        regions.append({
            "rings": rings, "color": col, "label": f"region_{i}",
            "area_mm2": m["area"] * (width_mm / img_w) * (height_mm / img_h),
            "iou": 1.0,
        })
    svg = regions_to_svg(regions, width_mm, height_mm, p)

    buf = io.BytesIO(svg.encode("utf-8"))
    buf.seek(0)
    stem = _NAMES.get(data["id"], "segment")
    return send_file(buf, mimetype="image/svg+xml", as_attachment=True,
                     download_name=f"{stem}_mask.svg")


@app.get("/")
def index():
    return _PAGE.replace("<!--TOOLZ-NAV-->", nav_html("segment"))


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>segment · laser-toolz</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><g fill='none' stroke='%23ff453a' stroke-width='4' stroke-linejoin='round'><path d='M6 40 L22 16 L34 34 L44 22 L58 44 L6 44 Z'/></g></svg>">
<style>
  :root {
    /* dark-first (matches server.py); light block below is the override */
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

  /* header + vector wordmark (red arrow = the "vector") */
  header { display:flex; align-items:center; gap:12px; padding:13px 22px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  .brand { margin:0; display:inline-flex; flex-direction:column; align-items:stretch; gap:3px; }
  .vec { display:flex; align-items:center; height:8px; padding:0 1px; }
  .vec .shaft { flex:1; height:1.5px; background:var(--acc); border-radius:1px; }
  .vec .head { width:0; height:0; margin-left:-1px; border-left:6px solid var(--acc);
               border-top:4px solid transparent; border-bottom:4px solid transparent; }
  .word { font-family:var(--mono); font-size:16px; font-weight:600; letter-spacing:-.01em; line-height:1;
          display:inline-flex; align-items:center; gap:5px; }
  .word .ideo { color:var(--acc); font-weight:400; font-size:15px; line-height:1; }
  .word .thin { color:var(--mut); font-weight:400; }
  .chip { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.14em;
          color:var(--mut); border:1px solid var(--line); border-radius:11px; padding:3px 9px; }

  .wrap { display:flex; flex:1; min-height:0; }
  .panel { width:320px; flex:none; background:var(--panel);
           overflow-y:auto; padding:16px 18px 26px; }
  .panel::-webkit-scrollbar { width:9px; }
  .panel::-webkit-scrollbar-thumb { background:var(--line); border-radius:9px; border:3px solid var(--panel); }

  /* draggable gutter between the sidebar and the stage (draws the divider hairline) */
  .gutter { flex:none; width:9px; align-self:stretch; position:relative; z-index:5; cursor:col-resize; }
  .gutter::after { content:""; position:absolute; top:0; bottom:0; left:50%; transform:translateX(-.5px);
                   width:1px; background:var(--line); transition:background .12s,width .12s; }
  .gutter:hover::after, .gutter.drag::after { background:var(--acc); width:2px; }
  body.col-resize, body.col-resize * { cursor:col-resize !important; user-select:none !important; }

  /* .stagewrap is the non-scrolling anchor so the zoom/stat overlays stay pinned
     while .stage scrolls; margin:auto centers the paper yet stays scrollable when zoomed */
  .stagewrap { position:relative; flex:1; min-width:0; display:flex; }
  .stage { flex:1; min-width:0; display:flex;
           background:var(--stage); overflow:auto; padding:32px; }
  .stage > .paper { margin:auto; }
  .paper { position:relative; line-height:0; background:var(--paper); border-radius:2px; box-shadow:var(--shadow); }
  #photo { display:block; max-width:74vw; max-height:82vh; width:auto; height:auto; }
  #overlay { position:absolute; left:0; top:0; cursor:crosshair; }
  .empty { color:var(--mut); padding:48px; font-size:12px; text-align:center; font-family:var(--mono); }

  /* zoom control — pinned bottom-center over the stage, a faint laser-red highlight
     so the always-visible pill reads as an active control */
  .zoom { position:absolute; left:50%; bottom:14px; transform:translateX(-50%); z-index:6;
          display:none; align-items:center; gap:1px; padding:3px;
          border:1px solid color-mix(in srgb,var(--acc) 42%,var(--line)); border-radius:9px;
          font-family:var(--mono); font-size:11px;
          background:color-mix(in srgb,var(--panel) 80%,transparent); backdrop-filter:blur(8px);
          box-shadow:0 2px 12px rgba(0,0,0,.28), 0 0 0 3px color-mix(in srgb,var(--acc) 12%,transparent); }
  .zoom.show { display:flex; }
  .zoom button { border:0; background:transparent; color:var(--fg); font:inherit; cursor:pointer;
          width:24px; height:22px; border-radius:6px; line-height:1;
          display:inline-flex; align-items:center; justify-content:center; }
  .zoom .home svg { width:12px; height:12px; }
  .zoom button:hover { background:color-mix(in srgb,var(--acc) 16%,var(--faint)); color:var(--acc); }
  .zoom .pct { width:52px; text-align:center; color:var(--mut); cursor:pointer; }
  .zoom .pct:hover { color:var(--acc); }

  /* groups — mono micro-headers with a trailing hairline rule */
  .grp { margin-top:15px; }
  .grp:first-of-type { margin-top:2px; }
  .grp h3 { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.14em;
            color:var(--mut); margin:0 0 9px; font-weight:600; display:flex; align-items:center; gap:8px; }
  .grp h3::after { content:""; flex:1; height:1px; background:var(--line); }

  label.row { display:flex; align-items:baseline; justify-content:space-between; gap:10px;
              margin:9px 0 3px; color:var(--fg); font-size:11.5px; font-family:var(--mono); letter-spacing:-.01em; }
  label.row:first-child { margin-top:0; }
  .hint { color:var(--mut); font-size:10.5px; margin:4px 0 0; font-family:var(--sans); }
  .blurb { color:var(--mut); font-size:11px; line-height:1.5; margin:0 0 8px; font-family:var(--sans); }
  .blurb:last-child { margin-bottom:0; }
  .blurb b { color:var(--fg); font-weight:600; }
  .blurb code { font-family:var(--mono); font-size:10px; color:var(--fg);
                background:var(--faint); border:1px solid var(--line); border-radius:4px; padding:0 4px; }

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

  /* checkbox row */
  .chk { display:flex; align-items:center; gap:9px; margin:11px 0 2px; font-size:11.5px; font-family:var(--mono); }
  .chk input { width:15px; height:15px; accent-color:var(--acc); cursor:pointer; margin:0; }
  .chk label { cursor:pointer; }

  /* buttons: .act (secondary) + .act.prim (high-contrast) */
  .act { width:100%; margin:7px 0 0; padding:8px 10px; border-radius:7px; border:1px solid var(--line);
         background:var(--field); color:var(--fg); cursor:pointer; font-family:var(--mono);
         font-size:12px; letter-spacing:.02em; transition:opacity .12s,transform .05s,border-color .12s,color .12s; }
  .act:hover:not(:disabled) { border-color:color-mix(in srgb,var(--acc) 45%,var(--line)); color:var(--fg); }
  .act:active:not(:disabled) { transform:translateY(1px); }
  .act:disabled { opacity:.4; cursor:not-allowed; }
  .act.prim { border-color:var(--fg); background:var(--fg); color:var(--bg); font-weight:600; }
  .act.prim:hover:not(:disabled) { opacity:.88; }

  .row2 { display:flex; gap:7px; }
  .row2 .act { flex:1; }

  hr.sep { border:0; border-top:1px solid var(--line); margin:15px 0 0; }
  .err { color:var(--err); font-size:11px; margin-top:9px; min-height:14px; font-family:var(--mono); }
  .count { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--fg); }

  /* upload dropzone — slim single-line bar with an upload glyph */
  .drop { display:flex; align-items:center; justify-content:center; gap:9px;
          border:1px dashed var(--line); border-radius:7px; padding:8px 12px; text-align:center;
          color:var(--mut); cursor:pointer; font-size:11.5px; font-family:var(--mono); line-height:1.35;
          background:var(--faint); transition:border-color .14s,background .14s,color .14s; }
  .drop:hover { border-color:var(--mut); color:var(--fg); }
  .drop.hot { border-color:var(--acc); color:var(--fg); background:color-mix(in srgb,var(--acc) 9%,var(--faint)); }
  .drop .ico { flex:none; width:15px; height:15px; transition:transform .16s; }
  .drop:hover .ico { transform:translateY(-2px); }

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
    <span class="word"><span class="ideo">⿴</span><b>segment</b></span>
  </h1>
  <span class="chip">segment · click-to-pick</span>
  <!--TOOLZ-NAV-->
</header>
<div class="wrap">
  <div class="panel">
    <div id="drop" class="drop"><svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15V4"/><path d="M7 9l5-5 5 5"/><path d="M5 20h14"/></svg><span>Drop an image, or click to upload &nbsp;<small>PNG / JPG</small></span></div>
    <input id="file" type="file" accept="image/*" class="hide">

    <div class="grp"><h3>Pick</h3>
      <p class="hint">Left-click = include · shift-click = exclude · then <b>Add region</b>.</p>
      <button id="addBtn" class="act prim" disabled>Add region</button>
      <button id="clearPts" class="act" disabled>Clear points</button>
      <div class="hint">regions picked: <span class="count" id="count">0</span></div>
      <div class="row2">
        <button id="undoBtn" class="act" disabled>Undo</button>
        <button id="resetBtn" class="act" disabled>Reset</button>
      </div>
    </div>

    <hr class="sep">

    <div class="grp"><h3>Output</h3>
      <label class="row"><span>width (mm)</span></label>
      <input type="number" id="width_mm" value="200" step="1">
      <button id="matchBtn" class="act" disabled>Match original image</button>
      <p class="hint">Sizes the SVG to the photo at Inkscape's 96&nbsp;DPI, so the
        region paths land exactly on the image when you import both — ready to use
        as a clip / mask.</p>
      <label class="row"><span>fill colour</span></label>
      <select id="color">
        <option value="label">distinct palette</option>
        <option value="mean">mean image colour</option>
        <option value="gray">gray ramp</option>
      </select>
      <label class="row"><span>fill opacity</span></label>
      <input type="number" id="opacity" value="1" min="0" max="1" step="0.1">
      <label class="row"><span>simplify (mm)</span></label>
      <input type="number" id="simplify" value="0.15" min="0" step="0.05">
      <div class="chk"><input type="checkbox" id="layers"><label for="layers">one Inkscape layer per region</label></div>
      <button id="dlBtn" class="act prim" disabled>Download SVG</button>
      <div class="err" id="status"></div>
    </div>

    <hr class="sep">

    <div class="grp"><h3>How it works</h3>
      <p class="blurb">On upload the image is encoded once by <b>MobileSAM</b> — a
        distilled Segment Anything model — into a dense feature embedding that's
        cached for the session.</p>
      <p class="blurb">Each click is a <i>prompt</i>: the decoder turns your
        include / exclude points into a probability mask over that embedding in a
        few milliseconds, so the outline tracks your clicks live. <b>Add region</b>
        freezes the current mask; the embedding is reused, so more picks cost nothing.</p>
      <p class="blurb">On download each frozen mask is traced to contours,
        simplified (<i>simplify&nbsp;mm</i>), and written as closed even-odd
        <code>&lt;path&gt;</code> rings at true millimetre scale — one labelled
        region each, ready for Inkscape.</p>
    </div>
  </div>

  <div class="gutter" id="gutter"></div>
  <div class="stagewrap">
    <div class="stage">
      <div class="paper" id="paper">
        <img id="photo" hidden><canvas id="overlay" hidden></canvas>
        <div class="empty" id="empty">Upload an image to begin…</div>
      </div>
    </div>
    <div class="zoom" id="zoom">
      <button class="home" id="zHome" title="Reset to 100%"><svg viewBox="0 0 16 16" fill="none"
        stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path
        d="M2.5 8 L8 3 L13.5 8"/><path d="M4 7.2 V13 H12 V7.2"/></svg></button>
      <button id="zOut" title="Zoom out">&minus;</button>
      <button class="pct" id="zPct" title="Reset to 100%">100%</button>
      <button id="zIn" title="Zoom in">+</button>
    </div>
    <div class="stat" id="stat"></div>
  </div>
</div>
<script>
let token=null, iw=0, ih=0, ow=0, oh=0, points=[], count=0;
const MM_PER_PX = 25.4 / 96;   // Inkscape imports bitmaps at 96 DPI (CSS px)
const photo=document.getElementById('photo'), ov=document.getElementById('overlay');
const ctx=ov.getContext('2d');
const $=id=>document.getElementById(id);
const status=m=>$('status').textContent=m;

async function upload(f){
  if(!f) return;
  status('uploading + encoding…');
  const fd=new FormData(); fd.append('image', f);
  const r=await (await fetch('/upload',{method:'POST',body:fd})).json();
  token=r.id; iw=r.w; ih=r.h; ow=r.orig_w; oh=r.orig_h; points=[]; count=0; $('count').textContent=0;
  $('matchBtn').disabled=!ow;
  photo.onload=()=>{ ov.width=iw; ov.height=ih;
    ov.style.width=photo.clientWidth+'px'; ov.style.height=photo.clientHeight+'px';
    photo.hidden=ov.hidden=false; $('empty').classList.add('hide'); draw(); __zoom.reset(); };
  photo.src='/image/'+token+'?t='+Date.now();
  status(''); $('stat').textContent='ready on '+r.device+' · '+iw+'×'+ih+' px — click an object';
  ['clearPts','resetBtn'].forEach(b=>$(b).disabled=false);
}

// dropzone: click to open the file picker, or drag an image onto it
$('drop').onclick=()=>$('file').click();
$('file').onchange=e=>upload(e.target.files[0]);
['dragenter','dragover'].forEach(ev=>$('drop').addEventListener(ev,e=>{
  e.preventDefault(); $('drop').classList.add('hot'); }));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{
  e.preventDefault(); $('drop').classList.remove('hot'); }));
$('drop').addEventListener('drop',e=>{ if(e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); });

window.addEventListener('resize',()=>{ if(token){ ov.style.width=photo.clientWidth+'px';
  ov.style.height=photo.clientHeight+'px'; }});

ov.onclick=async e=>{
  if(!token) return;
  const rect=ov.getBoundingClientRect();
  const x=Math.round((e.clientX-rect.left)/rect.width*iw);
  const y=Math.round((e.clientY-rect.top)/rect.height*ih);
  points.push([x,y, e.shiftKey?0:1]);
  await refresh();
};

async function refresh(){
  if(!points.length){ draw(); return; }
  status('predicting…');
  const r=await (await fetch('/pick',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:token, points})})).json();
  draw(r.polygons);
  $('addBtn').disabled=!(r.polygons&&r.polygons.length);
  status(r.area? ('mask: '+r.area+' px — Add region, or refine') : 'no mask; try another point');
}

function draw(polys){
  ctx.clearRect(0,0,iw,ih);
  if(polys){ ctx.lineWidth=Math.max(2,iw/400); ctx.strokeStyle='#ff453a';
    ctx.fillStyle='rgba(255,69,58,.20)';
    for(const poly of polys){ ctx.beginPath();
      poly.forEach((p,i)=> i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));
      ctx.closePath(); ctx.fill(); ctx.stroke(); } }
  for(const [x,y,l] of points){ ctx.beginPath();
    ctx.arc(x,y,Math.max(3,iw/220),0,6.28);
    ctx.fillStyle=l?'#31d158':'#0a84ff'; ctx.fill();
    ctx.lineWidth=2; ctx.strokeStyle='#fff'; ctx.stroke(); }
}

$('matchBtn').onclick=()=>{
  if(!ow) return;
  $('width_mm').value=(ow*MM_PER_PX).toFixed(2);
  status('width set to '+ow+'px @96dpi = '+(ow*MM_PER_PX).toFixed(1)+'mm');
};
$('clearPts').onclick=()=>{ points=[]; $('addBtn').disabled=true; draw(); status('points cleared'); };
$('addBtn').onclick=async()=>{
  const r=await (await fetch('/add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:token})})).json();
  count=r.count; $('count').textContent=count; points=[]; $('addBtn').disabled=true; draw();
  ['undoBtn','dlBtn'].forEach(b=>$(b).disabled=count===0);
  status('region added — pick the next object');
};
$('undoBtn').onclick=async()=>{
  const r=await (await fetch('/undo',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:token})})).json();
  count=r.count; $('count').textContent=count;
  ['undoBtn','dlBtn'].forEach(b=>$(b).disabled=count===0);
};
$('resetBtn').onclick=async()=>{
  await fetch('/reset',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:token})});
  count=0; points=[]; $('count').textContent=0; draw();
  ['undoBtn','dlBtn','addBtn'].forEach(b=>$(b).disabled=true); status('reset');
};
// server names the file <stem>_mask.svg; honor it, don't hardcode a constant
function dlName(resp, fb){ const m=/filename="?([^"]+)"?/.exec(resp.headers.get('Content-Disposition')||''); return m?m[1]:fb; }
$('dlBtn').onclick=async()=>{
  status('building SVG…');
  const body={id:token, width_mm:+$('width_mm').value, color:$('color').value,
    opacity:+$('opacity').value, simplify:+$('simplify').value, layers:$('layers').checked};
  const resp=await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  if(!resp.ok){ status('download failed'); return; }
  const blob=await resp.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=dlName(resp,'segment_mask.svg'); a.click();
  status(count+' regions downloaded');
};

// ---- draggable sidebar + image zoom (shared shape across the three toolz) ----
const __zoom = (function(){
  const panel=document.querySelector('.panel'), gutter=$('gutter');
  const MINW=240;
  const saved=parseInt(localStorage.getItem('lt_panel_w')||'',10);
  if(saved>=MINW) panel.style.width=Math.min(saved, window.innerWidth-300)+'px';
  let dragging=false;
  gutter.addEventListener('mousedown', e=>{ dragging=true; gutter.classList.add('drag');
    document.body.classList.add('col-resize'); e.preventDefault(); });
  window.addEventListener('mousemove', e=>{ if(!dragging) return;
    const maxW=Math.min(760, window.innerWidth-300);
    panel.style.width=Math.max(MINW, Math.min(maxW,
      Math.round(e.clientX-panel.getBoundingClientRect().left)))+'px'; });
  window.addEventListener('mouseup', ()=>{ if(!dragging) return; dragging=false;
    gutter.classList.remove('drag'); document.body.classList.remove('col-resize');
    localStorage.setItem('lt_panel_w', parseInt(panel.style.width,10)||''); });

  const paper=$('paper'), box=$('zoom'), pct=$('zPct');
  const MINZ=0.25, MAXZ=8; let z=1;
  function apply(){ paper.style.zoom=z; pct.textContent=Math.round(z*100)+'%'; }
  function setZoom(v){ z=Math.max(MINZ, Math.min(MAXZ,v)); apply(); }
  $('zIn').onclick=()=>setZoom(z*1.25);
  $('zOut').onclick=()=>setZoom(z/1.25);
  $('zHome').onclick=()=>setZoom(1);
  pct.onclick=()=>setZoom(1);
  document.querySelector('.stage').addEventListener('wheel', e=>{
    if(!(e.ctrlKey||e.metaKey) || !box.classList.contains('show')) return;
    e.preventDefault(); setZoom(z*(e.deltaY<0?1.1:1/1.1)); }, {passive:false});
  return { show(){ box.classList.add('show'); },
           reset(){ setZoom(1); box.classList.add('show'); },
           factor(){ return z; } };
})();
</script></body></html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))             # 5001 is server.py; 5000 is AirPlay
    print(f"segment_server on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)
