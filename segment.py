#!/usr/bin/env python3
"""
segment.py — turn a photo into an editable segmentation-mask SVG for Inkscape.

Unlike linify.py (which makes laser-ready HAIRLINE line art), this is a sibling
tool: it produces FILLED, multi-colour region shapes. That deliberately breaks
linify's "fill:none, single colour, tone-by-geometry" laser invariant, which is
exactly why it lives in its own file instead of as a linify --mode.

Pipeline:
  image ──► MobileSAM (SamAutomaticMaskGenerator) ──► N binary masks
        ──► per-mask contour trace (cv2, holes via even-odd) ──► mm-space paths
        ──► one labelled, filled <path> per region, optionally per-layer

Each region becomes a separately selectable object in Inkscape's Objects panel
(id + inkscape:label), so you can recolour, delete, or reshape masks by hand.
Coordinates are millimetres with a true-scale header, reusing linify's encoders
so a --width-mm 200 file imports at exactly 200 mm.

Usage:
  python segment.py photo.jpg -o mask.svg
  python segment.py photo.jpg -o mask.svg --max-regions 40 --layers
  python segment.py photo.jpg -o mask.svg --color mean --opacity 0.6

Needs: torch, opencv-python, mobile_sam (+ its weights). See --checkpoint.
"""

from __future__ import annotations

import argparse
import colorsys
import os
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

# Reuse linify's mm-grid path encoders so both tools speak the same coordinate
# language (relative moves on a 0.01 mm integer grid, true-scale unit header).
from linify import _MM_PER_UNIT, _fmt, _num, _pair, default_output_path, rdp

_DEFAULT_CHECKPOINT = os.path.join(os.path.dirname(__file__), "weights", "mobile_sam.pt")
_CHECKPOINT_URL = (
    "https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt"
)


def ensure_checkpoint(path: str = _DEFAULT_CHECKPOINT) -> str:
    """Return `path`, downloading the ~40MB MobileSAM checkpoint there if absent.

    Idempotent: a no-op when the file already exists. Streams to a sibling
    `.part` file and atomically renames on success, so an interrupted download
    never leaves a truncated `.pt` that would later fail to load.
    """
    if os.path.exists(path):
        return path
    import urllib.request

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    print(f"[segment] downloading MobileSAM checkpoint (~40MB) -> {path}", file=sys.stderr)
    try:
        with urllib.request.urlopen(_CHECKPOINT_URL) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length", 0))
            got = 0
            for chunk in iter(lambda: resp.read(1 << 16), b""):
                out.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r[segment]   {got >> 20}/{total >> 20} MB "
                          f"({got * 100 // total}%)", end="", file=sys.stderr)
        print("", file=sys.stderr)
        os.replace(tmp, path)
    except Exception as exc:                                # network / disk failure
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(
            f"Failed to download MobileSAM checkpoint from {_CHECKPOINT_URL}: {exc}\n"
            "Fetch it manually (~40MB):\n"
            f"  curl -L -o {path} {_CHECKPOINT_URL}"
        ) from exc
    return path


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass
class SegParams:
    """All tunables in one place (mirrors linify.Params' single-source pattern)."""

    # --- output geometry ---
    width_mm: float = 200.0          # physical output width (mm); height from aspect
    units: str = "mm"                # header unit: mm | cm | in
    resample: int = 1024             # working resolution, max dimension px (SAM input)

    # --- SAM automatic mask generation ---
    checkpoint: str = _DEFAULT_CHECKPOINT
    device: str = "auto"             # auto | cpu | mps | cuda
    points_per_side: int = 32        # denser grid = more/smaller regions, slower
    iou_thresh: float = 0.88         # drop masks below this predicted IoU
    stability_thresh: float = 0.92   # drop unstable masks
    min_area: float = 0.002          # drop masks smaller than this fraction of the image
    max_regions: int = 0             # keep only the N largest (0 = all)
    dedup_iou: float = 0.7           # drop a mask if IoU with a larger kept one exceeds this (0 = off)

    # --- vectorization ---
    simplify: float = 0.15           # contour decimation tolerance in mm (RDP)
    smooth_px: int = 1               # morphological open/close radius to de-noise masks (px)

    # --- styling ---
    color: str = "label"             # label (distinct palette) | mean (avg region colour) | gray
    opacity: float = 1.0             # fill-opacity 0..1
    stroke: str = "none"             # region outline colour ('none' or e.g. '#000')
    layers: bool = False             # each region in its own Inkscape layer (vs one group)


# --------------------------------------------------------------------------- #
# Image loading
# --------------------------------------------------------------------------- #
def load_rgb(source, resample: int) -> Tuple[np.ndarray, float]:
    """Load `source` as an HxWx3 uint8 RGB array at working resolution.

    Returns (rgb, aspect) with aspect = height/width of the ORIGINAL image.
    """
    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = img.convert("RGB")
    ow, oh = img.size
    aspect = oh / ow
    if resample and max(ow, oh) > resample:
        scale = resample / max(ow, oh)
        img = img.resize((max(1, round(ow * scale)), max(1, round(oh * scale))),
                         Image.LANCZOS)
    return np.asarray(img), aspect


# --------------------------------------------------------------------------- #
# Segmentation (MobileSAM)
# --------------------------------------------------------------------------- #
def _pick_device(pref: str):
    import torch
    if pref != "auto":
        return pref
    if torch.cuda.is_available():
        return "cuda"
    # MPS is intentionally NOT auto-picked: MobileSAM's SamAutomaticMaskGenerator
    # feeds float64 point tensors, which MPS rejects. Pass --device mps to try
    # anyway (it will fall back to CPU on the first float64 op).
    return "cpu"


def _dedup_masks(masks: List[dict], thresh: float) -> List[dict]:
    """Drop masks that near-duplicate a larger kept one (IoU > thresh).

    SAM's automatic generator emits nested masks — e.g. a whole shape AND a
    sub-part of it. Walking large→small and rejecting any mask whose IoU with an
    already-kept mask exceeds `thresh` keeps the most complete version of each
    region while discarding the redundant fragments. A bbox overlap pre-check
    skips the pixel intersection for the common disjoint case.
    """
    if thresh <= 0 or not masks:
        return masks
    kept: List[dict] = []
    kept_bool: List[np.ndarray] = []
    kept_box: List[Tuple[int, int, int, int]] = []
    for m in masks:                                        # already sorted large→small
        seg = m["segmentation"]
        x, y, w, h = (int(v) for v in m["bbox"])          # SAM bbox: x,y,w,h
        bx0, by0, bx1, by1 = x, y, x + w, y + h
        area = int(seg.sum())
        dup = False
        for k, kb, (kx0, ky0, kx1, ky1) in zip(kept, kept_bool, kept_box):
            if bx1 <= kx0 or kx1 <= bx0 or by1 <= ky0 or ky1 <= by0:
                continue                                   # bboxes disjoint → IoU 0
            inter = int(np.logical_and(seg, kb).sum())
            if inter == 0:
                continue
            union = area + int(k["area"]) - inter
            if union and inter / union > thresh:
                dup = True
                break
        if not dup:
            kept.append(m)
            kept_bool.append(seg)
            kept_box.append((bx0, by0, bx1, by1))
    return kept


def generate_masks(rgb: np.ndarray, p: SegParams) -> Tuple[List[dict], str]:
    """Run MobileSAM's automatic (grid-prompt) generator over the whole image.

    Returns SAM's raw mask dicts (segmentation bool array, area, bbox, scores),
    filtered by area/count and sorted large→small so big regions paint at the
    back and nested detail sits on top.
    """
    try:
        import warnings
        import torch
        with warnings.catch_warnings():                    # quiet MobileSAM's import
            warnings.filterwarnings(                       # banner: timm.models.layers/
                "ignore", category=FutureWarning, module=r"timm\..*")  # .registry are
            warnings.filterwarnings(                       # deprecated, and it re-registers
                "ignore", message="Overwriting .* in registry", category=UserWarning)
            from mobile_sam import sam_model_registry, SamAutomaticMaskGenerator
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError(
            "segment.py needs torch + mobile_sam:\n"
            "  pip install torch torchvision opencv-python timm\n"
            "  pip install git+https://github.com/ChaoningZhang/MobileSAM.git"
        ) from exc

    ensure_checkpoint(p.checkpoint)

    device = _pick_device(p.device)
    sam = sam_model_registry["vit_t"](checkpoint=p.checkpoint)
    h, w = rgb.shape[:2]
    min_region_px = int(p.min_area * h * w)

    def _build(dev):
        sam.to(dev)
        sam.eval()
        return SamAutomaticMaskGenerator(
            sam,
            points_per_side=p.points_per_side,
            pred_iou_thresh=p.iou_thresh,
            stability_score_thresh=p.stability_thresh,
            min_mask_region_area=min_region_px,
        )

    try:
        masks = _build(device).generate(rgb)
    except (RuntimeError, NotImplementedError, TypeError) as exc:
        if device != "cpu":                                # MPS/CUDA op gap → retry on CPU
            # (MobileSAM's auto generator uses float64 points, which MPS rejects)
            print(f"[segment] {device} failed ({exc}); falling back to CPU", file=sys.stderr)
            device = "cpu"
            masks = _build("cpu").generate(rgb)
        else:
            raise

    masks = [m for m in masks if m["area"] >= min_region_px]
    masks.sort(key=lambda m: m["area"], reverse=True)
    n_raw = len(masks)
    masks = _dedup_masks(masks, p.dedup_iou)
    if n_raw != len(masks):
        print(f"[segment] dedup dropped {n_raw - len(masks)} overlapping "
              f"mask(s) (IoU > {p.dedup_iou})", file=sys.stderr)
    if p.max_regions > 0 and len(masks) > p.max_regions:
        print(f"[segment] keeping {p.max_regions} largest of {len(masks)} regions",
              file=sys.stderr)
        masks = masks[:p.max_regions]
    return masks, device


# --------------------------------------------------------------------------- #
# Vectorization
# --------------------------------------------------------------------------- #
def _clean_mask(seg: np.ndarray, radius: int) -> np.ndarray:
    """Morphological open+close to shave single-pixel noise off a mask edge."""
    import cv2
    m = (seg.astype(np.uint8)) * 255
    if radius > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k)
    return m


def mask_to_rings(seg: np.ndarray, img_w: int, img_h: int, width_mm: float,
                  height_mm: float, p: SegParams) -> List[np.ndarray]:
    """Trace one binary mask to a list of closed mm-space rings (outer + holes).

    RETR_CCOMP gives outer boundaries and holes together; emitted as separate
    subpaths, they knock out under fill-rule="evenodd". Each ring is decimated
    in pixel space (approxPolyDP) then in mm (RDP) for a compact path.
    """
    import cv2
    m = _clean_mask(seg, p.smooth_px)
    contours, _ = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    sx = width_mm / img_w
    sy = height_mm / img_h
    eps_px = max(0.5, p.simplify / max(sx, sy))            # mm tolerance → px for approxPolyDP
    rings: List[np.ndarray] = []
    for c in contours:
        if len(c) < 3:
            continue
        c = cv2.approxPolyDP(c, eps_px, True).reshape(-1, 2).astype(np.float64)
        if len(c) < 3:
            continue
        ring = np.column_stack([c[:, 0] * sx, c[:, 1] * sy])
        ring = rdp(ring, p.simplify)
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _ring_d(ring: np.ndarray) -> str:
    """Closed relative-move subpath (same 0.01mm grid encoding as linify)."""
    _Q = 100
    pts = [(int(round(x * _Q)), int(round(y * _Q))) for x, y in ring]
    px, py = pts[0]
    d = "M" + _pair(px, py)
    for x, y in pts[1:]:
        d += "l" + _pair(x - px, y - py)
        px, py = x, y
    return d + "Z"


# --------------------------------------------------------------------------- #
# Colour
# --------------------------------------------------------------------------- #
def _palette(n: int) -> List[str]:
    """n visually distinct hex colours via golden-ratio hue stepping (deterministic)."""
    out = []
    h = 0.11
    for _ in range(n):
        r, g, b = colorsys.hsv_to_rgb(h % 1.0, 0.62, 0.92)
        out.append(f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}")
        h += 0.61803398875
    return out


def _mean_hex(rgb: np.ndarray, seg: np.ndarray) -> str:
    m = rgb[seg]
    if m.size == 0:
        return "#808080"
    r, g, b = m.mean(axis=0).astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


def region_colors(rgb: np.ndarray, masks: List[dict], p: SegParams) -> List[str]:
    if p.color == "mean":
        return [_mean_hex(rgb, m["segmentation"]) for m in masks]
    if p.color == "gray":
        n = max(1, len(masks))
        return [f"#{v:02x}{v:02x}{v:02x}"
                for v in (np.linspace(40, 220, n).astype(int))]
    return _palette(len(masks))                            # 'label' (default)


# --------------------------------------------------------------------------- #
# SVG serialization
# --------------------------------------------------------------------------- #
_INK = "http://www.inkscape.org/namespaces/inkscape"


def regions_to_svg(regions: List[dict], width_mm: float, height_mm: float,
                   p: SegParams) -> str:
    """Serialize traced regions to a filled, layered SVG for Inkscape.

    Each region: one <path fill-rule="evenodd"> with an id + inkscape:label,
    either wrapped in its own Inkscape layer (--layers) or collected under a
    single "segmentation" group. Header carries true physical size like linify.
    """
    per = _MM_PER_UNIT.get(p.units, 1.0)
    w, h = _fmt(width_mm), _fmt(height_mm)
    dw, dh = _fmt(width_mm / per), _fmt(height_mm / per)

    body = []
    for i, reg in enumerate(regions, 1):
        d = "".join(_ring_d(r) for r in reg["rings"])
        label = reg["label"]
        path = (
            f'<path id="region_{i}" inkscape:label="{label}" '
            f'd="{d}" fill="{reg["color"]}" fill-rule="evenodd" '
            f'fill-opacity="{_fmt(p.opacity)}" stroke="{p.stroke}"'
            + (f' stroke-width="{_fmt(0.2)}"' if p.stroke != "none" else "")
            + f' data-area-mm2="{_fmt(reg["area_mm2"])}"'
            + f' data-iou="{reg["iou"]:.3f}"/>'
        )
        if p.layers:
            body.append(
                f'  <g inkscape:groupmode="layer" id="layer_{i}" '
                f'inkscape:label="{label}">\n    {path}\n  </g>'
            )
        else:
            body.append("    " + path)

    if p.layers:
        inner = "\n".join(body)
    else:
        inner = ('  <g inkscape:groupmode="layer" id="segmentation" '
                 'inkscape:label="segmentation">\n' + "\n".join(body) + "\n  </g>")

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="{_INK}" version="1.1"\n'
        f'     width="{dw}{p.units}" height="{dh}{p.units}" viewBox="0 0 {w} {h}">\n'
        f"{inner}\n"
        f"</svg>\n"
    )


# --------------------------------------------------------------------------- #
# Top-level API
# --------------------------------------------------------------------------- #
def image_to_segmentation_svg(source, p: SegParams):
    """Render `source` (path or PIL.Image) to (svg_string, stats_dict)."""
    if p.units not in _MM_PER_UNIT:
        raise ValueError(f"unknown units {p.units!r}; choose from {list(_MM_PER_UNIT)}")

    rgb, aspect = load_rgb(source, p.resample)
    img_h, img_w = rgb.shape[:2]
    width_mm = float(p.width_mm)
    height_mm = width_mm * aspect

    masks, device = generate_masks(rgb, p)
    colors = region_colors(rgb, masks, p)

    regions = []
    total_pts = 0
    for i, (m, col) in enumerate(zip(masks, colors), 1):
        rings = mask_to_rings(m["segmentation"], img_w, img_h, width_mm, height_mm, p)
        if not rings:
            continue
        total_pts += sum(len(r) for r in rings)
        regions.append({
            "rings": rings,
            "color": col,
            "label": f"region_{i}",
            "area_mm2": float(m["area"]) * (width_mm / img_w) * (height_mm / img_h),
            "iou": float(m.get("predicted_iou", 0.0)),
        })

    svg = regions_to_svg(regions, width_mm, height_mm, p)
    stats = {
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "units": p.units,
        "regions": len(regions),
        "points": total_pts,
        "device": device,
    }
    return svg, stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    d = SegParams()
    ap = argparse.ArgumentParser(
        description="Convert an image into an editable segmentation-mask SVG (MobileSAM).")
    ap.add_argument("input", help="input raster image")
    ap.add_argument("-o", "--output",
                    help="output .svg path (default: <input>_mask.svg next to "
                         "the input; pass '-' for stdout)")

    ap.add_argument("--width-mm", type=float, default=d.width_mm, help="physical width (mm)")
    ap.add_argument("--units", default=d.units, choices=list(_MM_PER_UNIT), help="header unit")
    ap.add_argument("--resample", type=int, default=d.resample, help="working res, max dim px")

    ap.add_argument("--checkpoint", default=d.checkpoint, help="MobileSAM .pt weights")
    ap.add_argument("--device", default=d.device, choices=["auto", "cpu", "mps", "cuda"])
    ap.add_argument("--points-per-side", type=int, default=d.points_per_side,
                    help="SAM sampling grid density (more = more regions, slower)")
    ap.add_argument("--iou-thresh", type=float, default=d.iou_thresh)
    ap.add_argument("--stability-thresh", type=float, default=d.stability_thresh)
    ap.add_argument("--min-area", type=float, default=d.min_area,
                    help="drop regions below this fraction of image area")
    ap.add_argument("--max-regions", type=int, default=d.max_regions,
                    help="keep only the N largest regions (0 = all)")
    ap.add_argument("--dedup-iou", type=float, default=d.dedup_iou,
                    help="drop a mask if IoU with a larger kept one exceeds this (0 = off)")

    ap.add_argument("--simplify", type=float, default=d.simplify, help="RDP tolerance (mm)")
    ap.add_argument("--smooth-px", type=int, default=d.smooth_px, help="mask de-noise radius (px)")

    ap.add_argument("--color", default=d.color, choices=["label", "mean", "gray"],
                    help="region fill: distinct palette / mean image colour / grayscale ramp")
    ap.add_argument("--opacity", type=float, default=d.opacity, help="fill-opacity 0..1")
    ap.add_argument("--stroke", default=d.stroke, help="region outline colour ('none' or #hex)")
    ap.add_argument("--layers", action="store_true",
                    help="put each region in its own Inkscape layer")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    p = SegParams(
        width_mm=args.width_mm, units=args.units, resample=args.resample,
        checkpoint=args.checkpoint, device=args.device,
        points_per_side=args.points_per_side, iou_thresh=args.iou_thresh,
        stability_thresh=args.stability_thresh, min_area=args.min_area,
        max_regions=args.max_regions, dedup_iou=args.dedup_iou,
        simplify=args.simplify, smooth_px=args.smooth_px,
        color=args.color, opacity=args.opacity, stroke=args.stroke, layers=args.layers,
    )
    svg, stats = image_to_segmentation_svg(args.input, p)
    out = args.output or default_output_path(args.input, "mask")
    if out == "-":
        sys.stdout.write(svg)
    else:
        with open(out, "w") as f:
            f.write(svg)
        print(f"[segment] {stats['regions']} regions, {stats['points']} points, "
              f"{stats['width_mm']}×{stats['height_mm']}mm, device={stats['device']} "
              f"→ {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
