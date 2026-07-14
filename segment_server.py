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
    rgb, aspect = load_rgb(img, _RESAMPLE)
    token = secrets.token_urlsafe(8)
    h, w = rgb.shape[:2]
    _SESS[token] = {"rgb": rgb, "aspect": aspect, "accepted": []}
    _NAMES[token] = os.path.splitext(os.path.basename(f.filename or "segment"))[0] or "segment"
    if len(_SESS) > 8:                                      # cap memory (embeddings are large)
        for k in list(_SESS)[:-8]:
            _SESS.pop(k, None)
            _NAMES.pop(k, None)
    return jsonify(id=token, w=w, h=h, name=_NAMES[token], device=_get_predictor() and _DEVICE)


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
                     download_name=f"{stem}_seg.svg")


@app.get("/")
def index():
    return _PAGE


_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>segment — click-to-pick masks</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, sans-serif; margin: 0; display: flex; height: 100vh; }
  #side { width: 260px; padding: 16px; box-sizing: border-box; border-right: 1px solid #8884;
          overflow-y: auto; flex: none; }
  #main { flex: 1; overflow: auto; padding: 16px; display: flex; align-items: flex-start;
          justify-content: center; }
  #stage { position: relative; line-height: 0; }
  #photo { max-width: 100%; display: block; }
  #overlay { position: absolute; left: 0; top: 0; cursor: crosshair; }
  h1 { font-size: 15px; margin: 0 0 12px; }
  label { display: block; margin: 10px 0 2px; font-size: 12px; opacity: .8; }
  input[type=number], select { width: 100%; box-sizing: border-box; }
  button { display: block; width: 100%; margin: 6px 0; padding: 7px; cursor: pointer; }
  .prim { background: #2d6cdf; color: #fff; border: 0; border-radius: 5px; font-weight: 600; }
  .hint { font-size: 12px; opacity: .7; margin: 8px 0; }
  #count { font-weight: 600; }
</style></head>
<body>
<div id="side">
  <h1>segment · click-to-pick</h1>
  <input type="file" id="file" accept="image/*">
  <p class="hint">left-click = include · shift-click = exclude · then <b>Add region</b>.</p>
  <button id="addBtn" class="prim" disabled>Add region</button>
  <button id="clearPts" disabled>Clear points</button>
  <div class="hint">regions picked: <span id="count">0</span></div>
  <button id="undoBtn" disabled>Undo last region</button>
  <button id="resetBtn" disabled>Reset all</button>
  <hr>
  <label>width (mm)</label><input type="number" id="width_mm" value="200" step="1">
  <label>fill colour</label>
  <select id="color"><option value="label">distinct palette</option>
    <option value="mean">mean image colour</option><option value="gray">gray ramp</option></select>
  <label>fill opacity</label><input type="number" id="opacity" value="1" min="0" max="1" step="0.1">
  <label>simplify (mm)</label><input type="number" id="simplify" value="0.15" min="0" step="0.05">
  <label><input type="checkbox" id="layers"> one Inkscape layer per region</label>
  <button id="dlBtn" class="prim" disabled>Download SVG</button>
  <div class="hint" id="status"></div>
</div>
<div id="main"><div id="stage">
  <img id="photo" hidden><canvas id="overlay" hidden></canvas>
</div></div>
<script>
let token=null, iw=0, ih=0, points=[], count=0;
const photo=document.getElementById('photo'), ov=document.getElementById('overlay');
const ctx=ov.getContext('2d');
const $=id=>document.getElementById(id);
const status=m=>$('status').textContent=m;

$('file').onchange=async e=>{
  const f=e.target.files[0]; if(!f) return;
  status('uploading + encoding…');
  const fd=new FormData(); fd.append('image', f);
  const r=await (await fetch('/upload',{method:'POST',body:fd})).json();
  token=r.id; iw=r.w; ih=r.h; points=[]; count=0; $('count').textContent=0;
  photo.onload=()=>{ ov.width=iw; ov.height=ih;
    ov.style.width=photo.clientWidth+'px'; ov.style.height=photo.clientHeight+'px';
    photo.hidden=ov.hidden=false; draw(); };
  photo.src='/image/'+token+'?t='+Date.now();
  status('ready on '+r.device+' — click an object');
  ['clearPts','resetBtn'].forEach(b=>$(b).disabled=false);
};
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
  if(polys){ ctx.lineWidth=Math.max(2,iw/400); ctx.strokeStyle='#00e5ff';
    ctx.fillStyle='rgba(0,229,255,.25)';
    for(const poly of polys){ ctx.beginPath();
      poly.forEach((p,i)=> i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1]));
      ctx.closePath(); ctx.fill(); ctx.stroke(); } }
  for(const [x,y,l] of points){ ctx.beginPath();
    ctx.arc(x,y,Math.max(3,iw/220),0,6.28);
    ctx.fillStyle=l?'#31d158':'#ff453a'; ctx.fill();
    ctx.lineWidth=2; ctx.strokeStyle='#fff'; ctx.stroke(); }
}

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
$('dlBtn').onclick=async()=>{
  status('building SVG…');
  const body={id:token, width_mm:+$('width_mm').value, color:$('color').value,
    opacity:+$('opacity').value, simplify:+$('simplify').value, layers:$('layers').checked};
  const resp=await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  if(!resp.ok){ status('download failed'); return; }
  const blob=await resp.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download='segment_seg.svg'; a.click();
  status(count+' regions downloaded');
};
</script></body></html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5002"))             # 5001 is server.py; 5000 is AirPlay
    print(f"segment_server on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False)
