#!/usr/bin/env python3
"""
linify.py — convert a raster image into laser-ready hairline SVG line art.

Design rules (these exist because the output drives a laser, not a pen plotter):
  * Every line is a hairline. Stroke width is NOT a tonal variable — the laser
    ignores it. We emit one fixed tiny stroke-width, fill:none, one stroke color.
  * Tone is encoded by GEOMETRY only (displacement, spacing, or contours),
    never by line thickness or color.
  * Output is continuous paths (no dashes). Add perforation in the laser
    software.
  * Coordinate space is millimetres. The SVG carries width/height in mm plus a
    matching viewBox so it imports at true scale in LightBurn / Illustrator.

Three interchangeable render modes (``--mode``):
  wavy     displaced scanlines — darkness modulates the wiggle amplitude.
  spacing  density lines — lines pack together in dark regions.
  contour  topographic iso-brightness lines via skimage.measure.find_contours.

Usage:
  python linify.py INPUT.png -o OUT.svg --mode wavy [params...]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, fields
from typing import List, Optional

import numpy as np
from PIL import Image

# A hair below spacing/2 so two fully-dark adjacent wavy lines still keep a gap.
_MIN_WAVY_GAP_MM = 0.05

# Physical millimetres per display unit. All geometry stays in mm (the viewBox is
# always in mm); only the SVG width/height header is relabelled into the requested
# unit, so an inch-labelled file still imports at true physical scale in software
# that only speaks inches.
_MM_PER_UNIT = {"mm": 1.0, "cm": 10.0, "in": 25.4}


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass
class Params:
    """All tunables in one place so the CLI and the webserver share defaults."""

    mode: str = "wavy"

    # --- global geometry / output ---
    width_mm: float = 200.0          # physical output width (mm); height from aspect
    stroke_width: float = 0.02       # hairline, in mm (viewBox units)
    color: str = "black"             # single stroke color (e.g. 'red' for cut)
    units: str = "mm"                # header unit for width/height: mm | cm | in
    invert: bool = False             # flip tonal encoding (dark<->light)
    mask_threshold: Optional[float] = None  # skip where effective brightness > t
    samples: int = 800               # point density along each line
    decimate: float = 0.03           # collinear-point tolerance in mm (RDP)
    resample: int = 900              # working image resolution, max dimension px

    # --- wavy ---
    line_spacing: float = 2.0        # mm between scanline baselines
    amp: Optional[float] = None      # max wiggle amplitude mm (None => spacing/2)
    wavelength: float = 8.0          # carrier wavelength mm
    freq_mod: bool = False           # also raise spatial frequency in dark areas
    freq_amount: float = 1.0         # how strongly freq_mod bites (0..~2)

    # --- spacing ---
    min_spacing: float = 0.6         # mm between lines in the darkest regions
    max_spacing: float = 4.0         # mm between lines in the lightest regions

    # --- contour ---
    levels: int = 8                  # brightness quantization bands
    smooth: float = 0.0              # Gaussian sigma applied pre-contour (px)
    min_contour_len: float = 2.0     # drop contours shorter than this (mm)


# --------------------------------------------------------------------------- #
# Image loading / sampling
# --------------------------------------------------------------------------- #
def load_gray(source, resample: int, invert: bool):
    """Load `source` (path or PIL.Image) as a float grayscale array in [0,1].

    Returns (gray, aspect) where aspect = height/width of the ORIGINAL image.
    `gray` holds *effective* brightness: after --invert, 1.0 is the tone that
    reads as 'light' (small wiggle / sparse lines) and 0.0 as 'dark'.
    """
    img = source if isinstance(source, Image.Image) else Image.open(source)
    img = img.convert("L")
    ow, oh = img.size
    aspect = oh / ow

    # Downsample to a sane working resolution (keeps things fast + smooth).
    long_edge = max(ow, oh)
    if resample and long_edge > resample:
        scale = resample / long_edge
        img = img.resize((max(1, round(ow * scale)), max(1, round(oh * scale))),
                         Image.LANCZOS)

    gray = np.asarray(img, dtype=np.float64) / 255.0
    if invert:
        gray = 1.0 - gray
    return gray, aspect


def sample_grid(gray, xs_mm, ys_mm, width_mm, height_mm):
    """Bilinearly sample `gray` at the outer product of xs_mm x ys_mm.

    Returns an array of shape (len(ys_mm), len(xs_mm)).
    """
    h, w = gray.shape
    fx = np.clip(np.asarray(xs_mm) / width_mm * (w - 1), 0, w - 1)
    fy = np.clip(np.asarray(ys_mm) / height_mm * (h - 1), 0, h - 1)

    x0 = np.floor(fx).astype(int)
    x1 = np.minimum(x0 + 1, w - 1)
    y0 = np.floor(fy).astype(int)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = (fx - x0)[None, :]
    wy = (fy - y0)[:, None]

    # Gather the four neighbours for every (y,x) pair.
    g00 = gray[np.ix_(y0, x0)]
    g01 = gray[np.ix_(y0, x1)]
    g10 = gray[np.ix_(y1, x0)]
    g11 = gray[np.ix_(y1, x1)]

    top = g00 * (1 - wx) + g01 * wx
    bot = g10 * (1 - wx) + g11 * wx
    return top * (1 - wy) + bot * wy


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def rdp(points, eps):
    """Ramer-Douglas-Peucker decimation — drops near-collinear points.

    Keeps SVGs small without changing the visible curve. Iterative (no
    recursion limit worries on long scanlines).
    """
    n = len(points)
    if n < 3 or eps <= 0:
        return points
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        p0, p1 = points[i0], points[i1]
        seg = p1 - p0
        seg_len = float(np.hypot(seg[0], seg[1]))
        mids = points[i0 + 1:i1]
        if seg_len == 0.0:
            d = np.hypot(mids[:, 0] - p0[0], mids[:, 1] - p0[1])
        else:
            # perpendicular distance from each mid point to the p0->p1 line
            d = np.abs(seg[0] * (p0[1] - mids[:, 1])
                       - (p0[0] - mids[:, 0]) * seg[1]) / seg_len
        idx = int(np.argmax(d))
        if d[idx] > eps:
            mi = i0 + 1 + idx
            keep[mi] = True
            stack.append((i0, mi))
            stack.append((mi, i1))
    return points[keep]


def split_runs(pts, keep):
    """Split a polyline into contiguous runs where `keep` is True (for masking).

    Runs of fewer than 2 points are dropped (nothing to draw).
    """
    if keep.all():
        return [pts]
    runs = []
    n = len(keep)
    i = 0
    while i < n:
        if keep[i]:
            j = i
            while j < n and keep[j]:
                j += 1
            if j - i >= 2:
                runs.append(pts[i:j])
            i = j
        else:
            i += 1
    return runs


# --------------------------------------------------------------------------- #
# Mode: wavy — displaced scanlines
# --------------------------------------------------------------------------- #
def render_wavy(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Horizontal scanlines whose amplitude tracks darkness.

    y_out = y_base + A(b) * sin(phase(x)),  A = maxAmp * (1 - b)
    Dark => big wiggle (flip with --invert). maxAmp is clamped to < spacing/2
    so adjacent scanlines can never cross.
    """
    spacing = p.line_spacing
    n_lines = max(1, int(round(height_mm / spacing)))

    requested = p.amp if p.amp is not None else spacing / 2.0
    ceiling = max(0.0, spacing / 2.0 - _MIN_WAVY_GAP_MM)   # guarantees a gap
    max_amp = min(requested, ceiling)

    xs = np.linspace(0.0, width_mm, p.samples)
    dx = xs[1] - xs[0] if p.samples > 1 else width_mm

    ys_base = (np.arange(n_lines) + 0.5) * spacing
    bright = sample_grid(gray, xs, ys_base, width_mm, height_mm)  # (n_lines, samples)
    darkness = 1.0 - bright

    polylines = []
    for i in range(n_lines):
        d_row = darkness[i]
        amp = max_amp * d_row
        if p.freq_mod:
            # Higher spatial frequency in dark regions. Integrate the local
            # wavenumber so the wave stays phase-continuous across x.
            inv_wl = (1.0 / p.wavelength) * (1.0 + p.freq_amount * d_row)
            phase = 2.0 * np.pi * dx * np.cumsum(inv_wl)
        else:
            phase = 2.0 * np.pi * xs / p.wavelength
        y = ys_base[i] + amp * np.sin(phase)
        pts = np.column_stack([xs, y])

        if p.mask_threshold is not None:
            keep = bright[i] <= p.mask_threshold      # draw only over the subject
        else:
            keep = np.ones(len(xs), dtype=bool)

        for run in split_runs(pts, keep):
            polylines.append(rdp(run, p.decimate))
    return polylines


# --------------------------------------------------------------------------- #
# Mode: spacing — density lines
# --------------------------------------------------------------------------- #
def render_spacing(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Horizontal lines whose vertical density tracks darkness.

    We walk top->bottom in fine steps, accumulating dy / spacing(darkness).
    Each time the accumulator crosses 1.0 we emit a line: dark rows have small
    local spacing so the accumulator fills faster => lines pack closer.

    Honest tradeoff: this variant carries VERTICAL tonal detail well but is
    coarse HORIZONTALLY (each line is a full-width horizontal). That's expected
    and is the whole point of the mode — pick `contour` if you need form.
    """
    step = max(1e-4, min(p.min_spacing, p.max_spacing) / 6.0)  # fine vertical step
    n_rows = max(1, int(round(height_mm / step)))
    ys = (np.arange(n_rows) + 0.5) * step

    xs = np.linspace(0.0, width_mm, p.samples)
    bright = sample_grid(gray, xs, ys, width_mm, height_mm)     # (n_rows, samples)
    row_dark = np.clip(1.0 - bright.mean(axis=1), 0.0, 1.0)     # mean darkness / row

    # spacing_local: dark -> min_spacing, light -> max_spacing
    lo, hi = min(p.min_spacing, p.max_spacing), max(p.min_spacing, p.max_spacing)
    spacing_local = hi + (lo - hi) * row_dark

    polylines = []
    acc = 0.0
    for r in range(n_rows):
        acc += step / spacing_local[r]
        if acc >= 1.0:
            acc -= 1.0
            y = ys[r]
            pts = np.column_stack([xs, np.full_like(xs, y)])
            if p.mask_threshold is not None:
                keep = bright[r] <= p.mask_threshold
            else:
                keep = np.ones(len(xs), dtype=bool)
            for run in split_runs(pts, keep):
                polylines.append(rdp(run, p.decimate))
    return polylines


# --------------------------------------------------------------------------- #
# Mode: contour — topographic iso-lines
# --------------------------------------------------------------------------- #
def render_contour(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Iso-brightness contours — organic lines that follow the form.

    Quantize brightness into `levels` bands and trace each iso-line with
    skimage.measure.find_contours. --smooth blurs first to kill jaggies;
    --min-contour-len drops tiny noise loops.
    """
    from skimage import measure  # imported lazily: only this mode needs skimage

    img = gray
    if p.smooth > 0:
        from skimage.filters import gaussian
        img = gaussian(img, sigma=p.smooth)

    h, w = img.shape
    sx = width_mm / (w - 1) if w > 1 else width_mm     # mm per pixel-column
    sy = height_mm / (h - 1) if h > 1 else height_mm   # mm per pixel-row

    # Evenly spaced iso levels strictly inside the image's value range.
    lo, hi = float(img.min()), float(img.max())
    if hi - lo < 1e-6:
        return []
    levels = np.linspace(lo, hi, p.levels + 2)[1:-1]

    polylines = []
    for lv in levels:
        # Masking: a contour at level `lv` has brightness ~= lv everywhere, so
        # "skip where brightness > threshold" == skip contour levels above it.
        # This is what erases a near-white background.
        if p.mask_threshold is not None and lv > p.mask_threshold:
            continue
        for c in measure.find_contours(img, lv):
            # find_contours returns (row, col); convert to mm (x, y).
            pts = np.column_stack([c[:, 1] * sx, c[:, 0] * sy])
            seg = np.diff(pts, axis=0)
            length = float(np.hypot(seg[:, 0], seg[:, 1]).sum())
            if length < p.min_contour_len:
                continue
            polylines.append(rdp(pts, p.decimate))
    return polylines


_MODES = {
    "wavy": render_wavy,
    "spacing": render_spacing,
    "contour": render_contour,
}


# --------------------------------------------------------------------------- #
# SVG serialization
# --------------------------------------------------------------------------- #
def polylines_to_svg(polylines, width_mm, height_mm, p: Params) -> str:
    """Serialize mm-space polylines to a hairline SVG string."""
    def fmt(v):  # compact fixed-point; strips trailing zeros
        return f"{v:.3f}".rstrip("0").rstrip(".")

    paths = []
    for pl in polylines:
        if len(pl) < 2:
            continue
        d = "M" + " L".join(f"{fmt(x)} {fmt(y)}" for x, y in pl)
        paths.append(f'<path d="{d}"/>')

    # Coordinate space (paths + viewBox) is always mm; the physical size in the
    # header is the same length expressed in the requested unit. viewBox units need
    # not equal header units — the header sets true physical size, the viewBox is
    # just the internal grid — so the file imports at correct scale either way.
    per = _MM_PER_UNIT.get(p.units, 1.0)
    w, h = fmt(width_mm), fmt(height_mm)                  # viewBox / path units (mm)
    dw, dh = fmt(width_mm / per), fmt(height_mm / per)    # header, in display unit
    body = "\n".join(paths)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1"\n'
        f'     width="{dw}{p.units}" height="{dh}{p.units}" viewBox="0 0 {w} {h}">\n'
        f'  <g class="ink" fill="none" stroke="{p.color}" stroke-width="{p.stroke_width}"\n'
        f'     stroke-linecap="round" stroke-linejoin="round">\n'
        f"{body}\n"
        f"  </g>\n"
        f"</svg>\n"
    )


# --------------------------------------------------------------------------- #
# Top-level API (shared by CLI and webserver)
# --------------------------------------------------------------------------- #
def image_to_svg(source, p: Params):
    """Render `source` (path or PIL.Image) to (svg_string, stats_dict)."""
    if p.mode not in _MODES:
        raise ValueError(f"unknown mode {p.mode!r}; choose from {list(_MODES)}")
    if p.units not in _MM_PER_UNIT:
        raise ValueError(f"unknown units {p.units!r}; choose from {list(_MM_PER_UNIT)}")

    gray, aspect = load_gray(source, p.resample, p.invert)
    width_mm = float(p.width_mm)
    height_mm = width_mm * aspect

    polylines = _MODES[p.mode](gray, p, width_mm, height_mm)

    svg = polylines_to_svg(polylines, width_mm, height_mm, p)
    per = _MM_PER_UNIT.get(p.units, 1.0)
    stats = {
        "mode": p.mode,
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "units": p.units,
        "width_disp": round(width_mm / per, 4),
        "height_disp": round(height_mm / per, 4),
        "paths": len(polylines),
        "points": int(sum(len(pl) for pl in polylines)),
    }
    return svg, stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    d = Params()  # defaults source of truth
    ap = argparse.ArgumentParser(
        prog="linify.py",
        description="Convert a raster image into laser-ready hairline SVG line art.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("input", help="input raster image (PNG/JPG/...)")
    ap.add_argument("-o", "--output", help="output SVG path (default: stdout)")
    ap.add_argument("--mode", choices=list(_MODES), default=d.mode,
                    help="render mode")

    g = ap.add_argument_group("global")
    g.add_argument("--width-mm", type=float, default=d.width_mm,
                   help="physical output width in mm (height from aspect)")
    g.add_argument("--stroke-width", type=float, default=d.stroke_width,
                   help="hairline stroke width in mm")
    g.add_argument("--color", default=d.color,
                   help="stroke color, e.g. 'black' or 'red' for cut")
    g.add_argument("--units", choices=list(_MM_PER_UNIT), default=d.units,
                   help="unit for the SVG width/height header (--width-mm stays mm)")
    g.add_argument("--invert", action="store_true",
                   help="flip tonal encoding (dark<->light)")
    g.add_argument("--mask-threshold", type=float, default=d.mask_threshold,
                   help="skip drawing where effective brightness > this (0..1); "
                        "erases near-white backgrounds")
    g.add_argument("--samples", type=int, default=d.samples,
                   help="point density sampled along each line")
    g.add_argument("--decimate", type=float, default=d.decimate,
                   help="collinear-point removal tolerance in mm")
    g.add_argument("--resample", type=int, default=d.resample,
                   help="working image resolution, longest edge in px")

    w = ap.add_argument_group("wavy")
    w.add_argument("--line-spacing", type=float, default=d.line_spacing,
                   help="mm between scanline baselines")
    w.add_argument("--amp", type=float, default=d.amp,
                   help="max wiggle amplitude mm (default spacing/2, clamped)")
    w.add_argument("--wavelength", type=float, default=d.wavelength,
                   help="carrier wavelength in mm")
    w.add_argument("--freq-mod", action="store_true",
                   help="also raise spatial frequency in dark regions")
    w.add_argument("--freq-amount", type=float, default=d.freq_amount,
                   help="strength of --freq-mod (0..~2)")

    s = ap.add_argument_group("spacing")
    s.add_argument("--min-spacing", type=float, default=d.min_spacing,
                   help="mm between lines in the darkest regions")
    s.add_argument("--max-spacing", type=float, default=d.max_spacing,
                   help="mm between lines in the lightest regions")

    c = ap.add_argument_group("contour")
    c.add_argument("--levels", type=int, default=d.levels,
                   help="brightness quantization bands")
    c.add_argument("--smooth", type=float, default=d.smooth,
                   help="Gaussian blur sigma applied pre-contour (px)")
    c.add_argument("--min-contour-len", type=float, default=d.min_contour_len,
                   help="drop contours shorter than this (mm)")
    return ap


def params_from_args(ns) -> Params:
    kwargs = {}
    for f in fields(Params):
        if hasattr(ns, f.name):
            kwargs[f.name] = getattr(ns, f.name)
    return Params(**kwargs)


def main(argv=None) -> int:
    ns = build_parser().parse_args(argv)
    p = params_from_args(ns)
    try:
        svg, stats = image_to_svg(ns.input, p)
    except FileNotFoundError:
        print(f"error: input not found: {ns.input}", file=sys.stderr)
        return 1

    if ns.output:
        with open(ns.output, "w") as fh:
            fh.write(svg)
        print(
            f"wrote {ns.output}  [{stats['mode']}]  "
            f"{stats['width_mm']}x{stats['height_mm']}mm  "
            f"{stats['paths']} paths / {stats['points']} pts",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
