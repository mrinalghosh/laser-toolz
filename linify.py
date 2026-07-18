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

Six interchangeable render modes (``--mode``):
  wavy     displaced scanlines — darkness modulates the wiggle amplitude.
  spacing  density lines — lines pack together in dark regions.
  contour  topographic iso-brightness lines via skimage.measure.find_contours.
  filet    crochet grid — dark cells become filled squares on an open mesh.
  flow     edge-tangent hatching — short streamlines flow along the form.
  tsp      single continuous line — one traveling-salesman tour through a stipple.

Usage:
  python linify.py INPUT.png -o OUT.svg --mode wavy [params...]
"""

from __future__ import annotations

import argparse
import os
import re
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
    amp_gamma: float = 1.0           # amplitude response curve (<1 lifts midtones)
    phase_jitter: float = 0.0        # per-line phase decorrelation (0..1)
    wavelength: float = 8.0          # carrier wavelength mm
    freq_mod: bool = False           # also raise spatial frequency in dark areas
    freq_amount: float = 1.0         # how strongly freq_mod bites (0..~2)

    # --- spacing ---
    min_spacing: float = 0.6         # mm between lines in the darkest regions
    max_spacing: float = 4.0         # mm between lines in the lightest regions
    spacing_style: str = "clean"     # 'clean' (masked full lines) | 'density' (per-column)

    # --- contour ---
    levels: int = 8                  # brightness quantization bands
    smooth: float = 0.0              # blur sigma applied pre-contour (px)
    min_contour_len: float = 2.0     # drop contours shorter than this (mm)
    smooth_mode: str = "gaussian"    # pre-contour blur: 'gaussian' | 'bilateral' (edge-preserving)
    bilateral_color: float = 0.1     # bilateral tonal sigma (0..1); smaller = harder edges
    contour_source: str = "tone"     # trace on 'tone' (brightness) or 'edge' (gradient magnitude)
    min_contour_area: float = 0.0    # drop closed loops enclosing < this (mm^2); 0 = off
    max_contour_len: float = 0.0     # drop contours longer than this (mm); 0 = off
    islands_only: bool = False       # keep only closed loops (drop open/border contours)

    # --- filet (crochet grid) ---
    cells_wide: int = 60             # grid columns; rows derived to keep cells square
    fill_threshold: float = 0.5      # cell is "filled" when darkness >= this (0..1)
    fill_style: str = "x"            # filled-cell mark: 'x' | 'cross' | 'hatch'
    hatch_lines: int = 3             # parallel diagonals per cell when fill_style='hatch'
    mesh: bool = True                # draw the full grid lattice under the cells

    # --- flow (edge-tangent hatching) ---
    flow_smooth: float = 6.0         # structure-tensor blur sigma (px) — field coherence
    flow_spacing: float = 1.4        # mm between seed points (grid pitch)
    flow_len: float = 7.0            # mm arc length of each streamline stroke
    flow_step: float = 0.4           # mm integration step along a streamline
    flow_gamma: float = 1.0          # seed-density response curve (<1 lifts midtones)

    # --- tsp (single continuous line) ---
    points: int = 4000               # target stipple dot count (tour vertices)
    point_gamma: float = 1.0         # darkness weighting for dot density (<1 lifts midtones)
    tsp_improve: int = 2             # interleaved 2-opt + or-opt refinement passes (0 = raw Hilbert seed)

    # --- glyph (ascii / unicode line art) ---
    glyph_cols: int = 80             # grid columns; rows derived from cell aspect
    glyph_palette: str = "ascii"     # built-in ramp: ascii | blocks | boxdraw | favorites
    glyph_chars: str = ""            # explicit character set (overrides --glyph-palette)
    glyph_pack: str = "system"       # font pack: system | sans | mono | serif | unifont
    glyph_font: str = ""             # single font-file override (wins over --glyph-pack)
    glyph_gamma: float = 1.0         # darkness response curve (<1 lifts midtones)
    glyph_size: float = 0.92         # glyph size as a fraction of the cell (0..1)
    glyph_aspect: float = 1.0        # cell height/width ratio (1 = square cells)
    glyph_edge: float = 0.0          # edge-direction awareness 0..1 (0 = pure density)
    glyph_edge_threshold: float = 0.12  # min local edge coherence to allow a directional swap
    glyph_instance: bool = False     # emit each glyph once in <defs>, place with <use> (smaller file)
    glyph_density: str = "coverage"  # tonal-rank metric: coverage (ink area) | outline (stroke length)
    # palette-sheet export (glyph_palette_svg) — independent of any input image
    glyph_palette_cols: int = 16     # cells per row on the exported density-ramp sheet
    glyph_palette_cell: float = 8.0  # mm per glyph cell on the exported sheet


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


def sample_points(field, xs_mm, ys_mm, width_mm, height_mm):
    """Bilinearly sample `field` at *scattered* points (xs_mm[i], ys_mm[i]).

    Unlike `sample_grid` (outer product), this pairs xs and ys elementwise —
    what streamline tracing and stipple sampling need. Returns a 1-D array.
    """
    h, w = field.shape
    fx = np.clip(np.asarray(xs_mm, dtype=np.float64) / width_mm * (w - 1), 0, w - 1)
    fy = np.clip(np.asarray(ys_mm, dtype=np.float64) / height_mm * (h - 1), 0, h - 1)
    x0 = np.floor(fx).astype(int)
    x1 = np.minimum(x0 + 1, w - 1)
    y0 = np.floor(fy).astype(int)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = fx - x0
    wy = fy - y0
    f00 = field[y0, x0]
    f01 = field[y0, x1]
    f10 = field[y1, x0]
    f11 = field[y1, x1]
    top = f00 * (1 - wx) + f01 * wx
    bot = f10 * (1 - wx) + f11 * wx
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

    y_out = y_base + A(b) * sin(phase(x)),  A = maxAmp * darkness**amp_gamma
    Dark => big wiggle (flip with --invert). maxAmp is clamped to < spacing/2
    so adjacent scanlines can never cross.

    Two contrast levers, both pure geometry (stroke width is never touched):
      * amp_gamma < 1 lifts midtones up the amplitude curve, so mid-gray wiggles
        hard instead of rippling gently — the amplitude clamp alone gives weak
        contrast because a linear map only nears full swing at pure black.
      * phase_jitter offsets each scanline's phase so bulges stop lining up into
        vertical columns (the moiré/banding artifact of a shared phase).
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

    # Per-line phase offset: golden-ratio increments spread the offsets evenly
    # (and deterministically) around the circle, scaled by phase_jitter (0=off).
    _GOLDEN = 0.6180339887498949
    phase0 = 2.0 * np.pi * p.phase_jitter * ((np.arange(n_lines) * _GOLDEN) % 1.0)

    gamma = max(1e-6, p.amp_gamma)

    polylines = []
    for i in range(n_lines):
        d_row = np.clip(darkness[i], 0.0, 1.0)
        amp = max_amp * (d_row ** gamma if gamma != 1.0 else d_row)
        if p.freq_mod:
            # Higher spatial frequency in dark regions. Integrate the local
            # wavenumber so the wave stays phase-continuous across x.
            inv_wl = (1.0 / p.wavelength) * (1.0 + p.freq_amount * d_row)
            phase = phase0[i] + 2.0 * np.pi * dx * np.cumsum(inv_wl)
        else:
            phase = phase0[i] + 2.0 * np.pi * xs / p.wavelength
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
    """Horizontal lines whose density tracks darkness — two styles.

    We walk top->bottom in fine steps. Both styles share the idea that dark ⇒
    small local spacing ⇒ lines pack closer, but they differ in how they use the
    horizontal axis (the full 2D brightness field `bright`, not just its mean):

      clean   (B) Vertical packing is driven by each row's MEAN darkness, giving
                  crisp continuous horizontal lines. Each line is then clipped to
                  the columns that are locally dark enough to "want" a line at
                  that pitch, so lines break across light gaps and terminate at
                  the subject — recovering silhouette/form while staying clean.
      density (A) Every column carries its OWN accumulator, so ink lands per
                  (row, column): dark columns fire often, light columns rarely.
                  Adjacent same-tone columns fire on the same rows and fuse into
                  horizontal runs, so internal shading appears — at the cost of a
                  dithery / broken look near tonal boundaries.

    Neither touches stroke width; tone is entirely where-ink-lands (geometry).
    """
    step = max(1e-4, min(p.min_spacing, p.max_spacing) / 6.0)  # fine vertical step
    n_rows = max(1, int(round(height_mm / step)))
    ys = (np.arange(n_rows) + 0.5) * step

    xs = np.linspace(0.0, width_mm, p.samples)
    bright = sample_grid(gray, xs, ys, width_mm, height_mm)     # (n_rows, samples)

    # spacing_local(darkness): dark -> min_spacing, light -> max_spacing
    lo, hi = min(p.min_spacing, p.max_spacing), max(p.min_spacing, p.max_spacing)
    dark2d = np.clip(1.0 - bright, 0.0, 1.0)                    # per (row, col)
    s_col = hi + (lo - hi) * dark2d                             # local spacing / col

    polylines = []

    if p.spacing_style == "density":
        # A — per-column accumulators; emit whatever columns cross on each row.
        acc = np.zeros(len(xs))
        for r in range(n_rows):
            acc += step / s_col[r]
            fire = acc >= 1.0
            acc[fire] -= 1.0
            if p.mask_threshold is not None:
                fire = fire & (bright[r] <= p.mask_threshold)
            if not fire.any():
                continue
            pts = np.column_stack([xs, np.full_like(xs, ys[r])])
            for run in split_runs(pts, fire):
                polylines.append(rdp(run, p.decimate))
        return polylines

    # B (default 'clean') — row-mean packing, each line clipped to columns that
    # locally want a line at least this dense (s_col <= the row's pitch).
    row_pitch = hi + (lo - hi) * dark2d.mean(axis=1)            # per-row spacing
    acc = 0.0
    for r in range(n_rows):
        acc += step / row_pitch[r]
        if acc >= 1.0:
            acc -= 1.0
            keep = s_col[r] <= row_pitch[r] + 1e-9     # dark-enough columns
            if p.mask_threshold is not None:
                keep = keep & (bright[r] <= p.mask_threshold)
            pts = np.column_stack([xs, np.full_like(xs, ys[r])])
            for run in split_runs(pts, keep):
                polylines.append(rdp(run, p.decimate))
    return polylines


# --------------------------------------------------------------------------- #
# Mode: contour — topographic iso-lines
# --------------------------------------------------------------------------- #
def render_contour(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Iso-brightness contours — organic lines that follow the form.

    Quantize the contour field into `levels` bands and trace each iso-line with
    skimage.measure.find_contours. --smooth blurs first to kill jaggies
    (--smooth-mode bilateral preserves edges); --contour-source edge traces the
    gradient magnitude instead of brightness. --min-contour-len / --max-contour-len
    / --min-contour-area / --islands-only curate which contours survive.
    """
    from skimage import measure  # imported lazily: only this mode needs skimage

    img = gray
    if p.smooth > 0:
        if p.smooth_mode == "bilateral":
            # Edge-preserving: flattens flat regions (kills garbage contours)
            # while keeping boundaries crisp, so iso-lines snap hard to edges.
            from skimage.restoration import denoise_bilateral
            img = denoise_bilateral(img, sigma_color=p.bilateral_color,
                                    sigma_spatial=p.smooth, channel_axis=None)
        else:
            from skimage.filters import gaussian
            img = gaussian(img, sigma=p.smooth)

    # The field we trace: brightness (tone) or gradient magnitude (edge).
    field = img
    if p.contour_source == "edge":
        from skimage.filters import sobel
        field = sobel(img)

    h, w = field.shape
    sx = width_mm / (w - 1) if w > 1 else width_mm     # mm per pixel-column
    sy = height_mm / (h - 1) if h > 1 else height_mm   # mm per pixel-row

    # Evenly spaced iso levels strictly inside the field's value range.
    lo, hi = float(field.min()), float(field.max())
    if hi - lo < 1e-6:
        return []
    levels = np.linspace(lo, hi, p.levels + 2)[1:-1]

    polylines = []
    for lv in levels:
        # Masking is tonal (erases a near-white background): a contour at level
        # `lv` has brightness ~= lv everywhere, so "skip where brightness >
        # threshold" == skip levels above it. Meaningless on the edge field, so
        # only applied when tracing tone.
        if (p.contour_source == "tone" and p.mask_threshold is not None
                and lv > p.mask_threshold):
            continue
        for c in measure.find_contours(field, lv):
            # find_contours returns (row, col); convert to mm (x, y).
            pts = np.column_stack([c[:, 1] * sx, c[:, 0] * sy])
            # A loop is "closed" when its endpoints coincide; open contours
            # touch the image border and have no meaningful enclosed area.
            closed = len(pts) >= 4 and np.allclose(pts[0], pts[-1])
            if p.islands_only and not closed:
                continue
            seg = np.diff(pts, axis=0)
            length = float(np.hypot(seg[:, 0], seg[:, 1]).sum())
            if length < p.min_contour_len:
                continue
            if p.max_contour_len > 0 and length > p.max_contour_len:
                continue
            if p.min_contour_area > 0 and closed:
                x, y = pts[:, 0], pts[:, 1]           # shoelace area (mm^2)
                area = 0.5 * abs(float(np.dot(x, np.roll(y, -1))
                                       - np.dot(y, np.roll(x, -1))))
                if area < p.min_contour_area:
                    continue
            polylines.append(rdp(pts, p.decimate))
    return polylines


# --------------------------------------------------------------------------- #
# Mode: filet — crochet grid (filled vs. open cells)
# --------------------------------------------------------------------------- #
def render_filet(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Filet-crochet chart: quantize the image to a grid of filled / open cells.

    The image is split into square-ish cells (``--cells-wide`` columns, rows
    derived so cells stay square). A cell is "filled" when its mean darkness
    clears ``--fill-threshold``; open cells stay empty mesh windows. Tone is
    binary and purely geometric — a filled cell carries a *mark* (X / cross /
    hatch), never a heavier stroke.

    The mesh lattice (every cell border) is drawn as long continuous horizontal
    and vertical hairlines, mirroring real filet mesh and minimising laser
    travel. ``--no-mesh`` — or enabling the background mask — drops the lattice
    so only filled cells produce geometry, each with its own outline, floating on
    blank ground.
    """
    cols = max(1, int(p.cells_wide))
    cw = width_mm / cols
    rows = max(1, int(round(height_mm / cw)))     # keep cells ~square
    ch = height_mm / rows

    # Per-cell mean brightness: supersample with bilinear taps, average per cell.
    ss = 3                                          # taps per cell edge
    xs = (np.arange(cols * ss) + 0.5) * (cw / ss)
    ys = (np.arange(rows * ss) + 0.5) * (ch / ss)
    fine = sample_grid(gray, xs, ys, width_mm, height_mm)      # (rows*ss, cols*ss)
    bright = fine.reshape(rows, ss, cols, ss).mean(axis=(1, 3))  # (rows, cols)
    darkness = np.clip(1.0 - bright, 0.0, 1.0)

    filled = darkness >= p.fill_threshold
    if p.mask_threshold is not None:
        filled &= bright <= p.mask_threshold        # erase light background cells

    # The mask (or --no-mesh) hides the full lattice; without it, each filled
    # cell needs its own border box to stay legible on blank ground.
    draw_mesh = p.mesh and p.mask_threshold is None

    polylines: List[np.ndarray] = []

    if draw_mesh:
        for j in range(rows + 1):                   # continuous horizontal bars
            y = j * ch
            polylines.append(np.array([[0.0, y], [width_mm, y]]))
        for i in range(cols + 1):                   # continuous vertical bars
            x = i * cw
            polylines.append(np.array([[x, 0.0], [x, height_mm]]))

    def hatch_chords(x0, y0):
        """Evenly spaced 45° chords clipped to the cell — count = tonal density."""
        n = max(1, int(p.hatch_lines))
        out = []
        for k in range(1, n + 1):
            c = -1.0 + 2.0 * k / (n + 1)            # slope-1 offset in unit square
            u_lo, u_hi = max(0.0, -c), min(1.0, 1.0 - c)
            if u_hi - u_lo <= 1e-9:
                continue
            out.append(np.array([[x0 + u_lo * cw, y0 + (u_lo + c) * ch],
                                 [x0 + u_hi * cw, y0 + (u_hi + c) * ch]]))
        return out

    for j, i in zip(*np.nonzero(filled)):
        x0, y0 = i * cw, j * ch
        x1, y1 = x0 + cw, y0 + ch
        if not draw_mesh:                            # own outline (no lattice)
            polylines.append(np.array([[x0, y0], [x1, y0], [x1, y1],
                                       [x0, y1], [x0, y0]]))
        if p.fill_style == "hatch":
            polylines.extend(hatch_chords(x0, y0))
        elif p.fill_style == "cross":                # plus: mid vertical + horizontal
            xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            polylines.append(np.array([[xm, y0], [xm, y1]]))
            polylines.append(np.array([[x0, ym], [x1, ym]]))
        else:                                        # 'x' — two full diagonals
            polylines.append(np.array([[x0, y0], [x1, y1]]))
            polylines.append(np.array([[x0, y1], [x1, y0]]))

    return polylines


# --------------------------------------------------------------------------- #
# Mode: flow — edge-tangent flow-field hatching
# --------------------------------------------------------------------------- #
def _flow_tangent_field(gray, sigma):
    """Unit tangent field (tx, ty) that flows ALONG edges (isophotes).

    Built from the *smoothed structure tensor*. The eigenvector of its smaller
    eigenvalue points where intensity changes least — i.e. along the edge — so
    the field wraps the form and stays coherent even where the raw gradient is
    pure noise (flat regions). This is what makes the strokes read as an
    illustrator's contour hatching rather than random scribble.
    """
    from skimage.filters import gaussian  # optional dep, shared with contour mode

    g = gaussian(gray, sigma=max(0.5, sigma * 0.5))     # denoise before deriving
    gy, gx = np.gradient(g)                             # row = y, col = x
    # Structure-tensor components, each smoothed to average orientations locally.
    sxx = gaussian(gx * gx, sigma=sigma)
    syy = gaussian(gy * gy, sigma=sigma)
    sxy = gaussian(gx * gy, sigma=sigma)
    # Dominant gradient orientation; the tangent is perpendicular to it (+90°).
    theta = 0.5 * np.arctan2(2.0 * sxy, sxx - syy)
    tx = -np.sin(theta)
    ty = np.cos(theta)
    return tx, ty


def render_flow(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Flow-field hatching — short streamlines that flow along the form.

    A coherent tangent field (see `_flow_tangent_field`) is seeded on a jittered
    grid; each seed is kept with probability darkness**flow_gamma, so strokes
    crowd into shadows and thin out in highlights. Tone is therefore encoded as
    *stroke density* (geometry), never stroke width. Each kept seed grows a
    streamline of arc length `flow_len` by integrating the field both ways from
    the seed, so the seed sits at the stroke's midpoint.
    """
    tx, ty = _flow_tangent_field(gray, p.flow_smooth)

    # Jittered grid of seeds at `flow_spacing` pitch (jitter breaks grid moire).
    rng = np.random.default_rng(0)                      # deterministic per params
    pitch = max(1e-3, p.flow_spacing)
    nx = max(1, int(round(width_mm / pitch)))
    ny = max(1, int(round(height_mm / pitch)))
    gx_c = (np.arange(nx) + 0.5) * (width_mm / nx)
    gy_c = (np.arange(ny) + 0.5) * (height_mm / ny)
    sx, sy = np.meshgrid(gx_c, gy_c)
    sx = sx.ravel() + (rng.random(nx * ny) - 0.5) * pitch
    sy = sy.ravel() + (rng.random(nx * ny) - 0.5) * pitch
    sx = np.clip(sx, 0.0, width_mm)
    sy = np.clip(sy, 0.0, height_mm)

    # Keep seeds probabilistically by local darkness -> tonal density.
    bright_seed = sample_points(gray, sx, sy, width_mm, height_mm)
    gamma = max(1e-6, p.flow_gamma)
    dark_seed = np.clip(1.0 - bright_seed, 0.0, 1.0) ** gamma
    keep = rng.random(len(sx)) < dark_seed
    if p.mask_threshold is not None:
        keep &= bright_seed <= p.mask_threshold
    sx, sy = sx[keep], sy[keep]
    if len(sx) == 0:
        return []

    # Vectorized streamline integration: every stroke advances together.
    step = max(1e-3, p.flow_step)
    half = max(1, int(round((p.flow_len * 0.5) / step)))

    def integrate(direction):
        """March `half` steps from every seed; return list of (M,2) positions."""
        X, Y = sx.copy(), sy.copy()
        pdx, pdy = None, None
        out = []
        for _ in range(half):
            vx = sample_points(tx, X, Y, width_mm, height_mm)
            vy = sample_points(ty, X, Y, width_mm, height_mm)
            if pdx is None:
                vx, vy = vx * direction, vy * direction   # pick initial sense
            else:
                # Eigenvector sign is ambiguous; align with the previous step
                # so the streamline never doubles back on itself.
                flip = (vx * pdx + vy * pdy) < 0.0
                vx = np.where(flip, -vx, vx)
                vy = np.where(flip, -vy, vy)
            norm = np.hypot(vx, vy) + 1e-12
            vx, vy = vx / norm, vy / norm
            X = np.clip(X + vx * step, 0.0, width_mm)
            Y = np.clip(Y + vy * step, 0.0, height_mm)
            pdx, pdy = vx, vy
            out.append(np.column_stack([X, Y]))
        return out

    fwd = integrate(+1.0)                               # seed -> forward
    back = integrate(-1.0)                              # seed -> backward
    seed_col = np.column_stack([sx, sy])
    # Stitch: reversed backward half + seed + forward half, per streamline.
    stack = back[::-1] + [seed_col] + fwd               # list of (M,2), length 2*half+1
    traj = np.stack(stack, axis=1)                      # (M, 2*half+1, 2)

    polylines = []
    for m in range(traj.shape[0]):
        pts = traj[m]
        if p.mask_threshold is not None:
            b = sample_points(gray, pts[:, 0], pts[:, 1], width_mm, height_mm)
            for run in split_runs(pts, b <= p.mask_threshold):
                polylines.append(rdp(run, p.decimate))
        else:
            polylines.append(rdp(pts, p.decimate))
    return polylines


# --------------------------------------------------------------------------- #
# Mode: tsp — single continuous line (stipple + traveling-salesman tour)
# --------------------------------------------------------------------------- #
def _hilbert_order(P, width_mm, height_mm, bits=16):
    """Seed the tour by sorting dots along a Hilbert space-filling curve.

    Unlike a greedy nearest-neighbor tour, a Hilbert ordering *never* makes a
    long jump: points that are close in the plane stay close in the ordering,
    so the initial path has no strands to reach back for. 2-opt / or-opt then
    polish the local detail. This is what kills the radiating spikes a pure
    nearest-neighbor seed leaves behind.
    """
    n = 1 << bits                                       # side of the integer grid
    x = np.clip(P[:, 0] / max(width_mm, 1e-9) * (n - 1), 0, n - 1).astype(np.int64)
    y = np.clip(P[:, 1] / max(height_mm, 1e-9) * (n - 1), 0, n - 1).astype(np.int64)
    d = np.zeros(len(P), dtype=np.int64)
    s = n >> 1
    while s > 0:                                        # standard xy->d, vectorized
        rx = ((x & s) > 0).astype(np.int64)
        ry = ((y & s) > 0).astype(np.int64)
        d += s * s * ((3 * rx) ^ ry)
        flip = (ry == 0) & (rx == 1)                    # rotate quadrant when ry==0
        x = np.where(flip, (n - 1) - x, x)
        y = np.where(flip, (n - 1) - y, y)
        swap = ry == 0
        x, y = np.where(swap, y, x), np.where(swap, x, y)
        s >>= 1
    return np.argsort(d, kind="stable")


def _two_opt(P, order, tree, passes, k=8):
    """Neighbor-limited 2-opt: uncross the tour using each node's k neighbors.

    Full 2-opt is O(n^2) per pass; restricting reconnection candidates to the
    k nearest neighbors makes it ~O(n*k) and removes the ugly long crossings a
    pure nearest-neighbor tour leaves behind.
    """
    n = len(P)
    if passes <= 0 or n < 4:
        return order
    _, nbrs = tree.query(P, k=min(k + 1, n))
    nbrs = np.atleast_2d(nbrs)
    for _ in range(passes):
        pos = np.empty(n, dtype=int)
        pos[order] = np.arange(n)
        improved = False
        for a in range(n - 1):
            i, i_next = order[a], order[a + 1]
            pi, pin = P[i], P[i_next]
            d_cur = np.hypot(*(pi - pin))
            for j in nbrs[i][1:]:
                b = pos[j]
                if b <= a + 1 or b >= n - 1:
                    continue
                jn = order[b + 1]
                # Reverse segment a+1..b: edges (i,i_next)+(j,jn) -> (i,j)+(i_next,jn)
                d_old = d_cur + np.hypot(*(P[j] - P[jn]))
                d_new = np.hypot(*(pi - P[j])) + np.hypot(*(pin - P[jn]))
                if d_new + 1e-9 < d_old:
                    order[a + 1:b + 1] = order[a + 1:b + 1][::-1]
                    pos[order] = np.arange(n)
                    improved = True
                    i_next = order[a + 1]
                    pin = P[i_next]
                    d_cur = np.hypot(*(pi - pin))
        if not improved:
            break
    return order


def _or_opt(P, order, tree, passes, k=8, max_seg=3):
    """Relocate short runs (length 1..max_seg) next to a nearer node.

    Or-opt complements 2-opt: 2-opt can only *reverse* a span, so the "detour
    out to grab one stray dot, then jump back" edges survive it. Or-opt lifts
    that short run out and reinserts it beside one of its true neighbors,
    closing the gap it left behind — exactly the move that removes those jumps.
    Candidate insertion edges are limited to each run-end's k nearest neighbors
    to stay ~O(n*k) per pass.
    """
    n = len(P)
    if passes <= 0 or n < 5:
        return order
    _, nbrs = tree.query(P, k=min(k + 1, n))
    nbrs = np.atleast_2d(nbrs)

    def dist(u, v):                                     # -1 = past a path end
        if u < 0 or v < 0:
            return 0.0
        return float(np.hypot(P[u, 0] - P[v, 0], P[u, 1] - P[v, 1]))

    order = list(int(i) for i in order)
    for _ in range(passes):
        pos = np.empty(n, dtype=int)
        pos[order] = np.arange(n)
        improved = False
        a = 0
        while a < n:
            moved = False
            for L in range(1, max_seg + 1):
                if a + L > n:
                    break
                s0, s1 = order[a], order[a + L - 1]     # run endpoints
                prev = order[a - 1] if a > 0 else -1
                nxt = order[a + L] if a + L < n else -1
                # gain from splicing the run out and bridging prev<->nxt
                gain = dist(prev, s0) + dist(s1, nxt) - dist(prev, nxt)
                if gain <= 1e-9:
                    continue
                best_delta, best_c, best_rev = 1e-9, -1, False
                for node in set(nbrs[s0][1:]).union(nbrs[s1][1:]):
                    c = int(pos[node])
                    if a - 1 <= c <= a + L - 1:         # the run's own edges
                        continue
                    cn = order[c + 1] if c + 1 < n else -1
                    base = dist(order[c], cn)
                    # insert forward (c-s0..s1-cn) or reversed (c-s1..s0-cn)
                    delta_f = gain + base - dist(order[c], s0) - dist(s1, cn)
                    delta_r = gain + base - dist(order[c], s1) - dist(s0, cn)
                    if delta_f > best_delta:
                        best_delta, best_c, best_rev = delta_f, c, False
                    if delta_r > best_delta:
                        best_delta, best_c, best_rev = delta_r, c, True
                if best_c != -1:
                    seg = order[a:a + L]
                    if best_rev:
                        seg = seg[::-1]
                    del order[a:a + L]
                    c = best_c if best_c < a else best_c - L
                    order[c + 1:c + 1] = seg
                    pos[order] = np.arange(n)
                    improved = moved = True
                    break
            a += 1
        if not improved:
            break
    return np.asarray(order, dtype=int)


def render_tsp(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """Whole image as ONE continuous line — the single-stroke portrait.

    Two stages, both pure geometry:
      1. Stipple: reject-sample points with acceptance = darkness**point_gamma,
         so dot density tracks tone (dense in shadow, sparse in light).
      2. Tour: seed one path by sorting the dots along a Hilbert curve (no
         long jumps by construction), then refine with interleaved 2-opt
         (uncross) and or-opt (relocate strays) passes. The result is a single
         unbroken polyline — one cut path for the whole image.
    """
    try:
        from scipy.spatial import cKDTree
    except ImportError as exc:                          # pragma: no cover
        raise RuntimeError("tsp mode needs scipy (installed with scikit-image); "
                           "pip install scipy") from exc

    rng = np.random.default_rng(0)                      # deterministic per params
    target = max(2, int(p.points))
    gamma = max(1e-6, p.point_gamma)

    # Reject-sample in batches until we have `target` accepted dots (or give up).
    collected = []
    have = 0
    attempts = 0
    batch = max(1000, target * 4)
    max_attempts = target * 80
    while have < target and attempts < max_attempts:
        cx = rng.random(batch) * width_mm
        cy = rng.random(batch) * height_mm
        b = sample_points(gray, cx, cy, width_mm, height_mm)
        acc = rng.random(batch) < np.clip(1.0 - b, 0.0, 1.0) ** gamma
        if p.mask_threshold is not None:
            acc &= b <= p.mask_threshold
        if acc.any():
            collected.append(np.column_stack([cx[acc], cy[acc]]))
            have += int(acc.sum())
        attempts += batch

    if not collected:
        return []
    P = np.vstack(collected)[:target]
    if len(P) < 2:
        return []

    tree = cKDTree(P)
    order = _hilbert_order(P, width_mm, height_mm)
    for _ in range(max(0, int(p.tsp_improve))):         # 2-opt & or-opt feed each other
        order = _two_opt(P, order, tree, 1)
        order = _or_opt(P, order, tree, 1)
    return [rdp(P[order], p.decimate)]


# --------------------------------------------------------------------------- #
# Mode: glyph — ASCII / Unicode line art (density ramp + edge-aware substitution)
# --------------------------------------------------------------------------- #
# The glyphs in a font are ALREADY vector outlines; freetype-py hands us the
# font's own curves (no tracing) plus a rasterizer we reuse to rank each glyph
# by ink density and to tag its dominant stroke orientation. Output stays pure
# geometry: each cell places the chosen glyph's outline as hairline polylines,
# tone encoded by which glyph (how much contour packs into the cell), never by
# thickness — same laser rule as every other mode.
# Font packs: named freetype stacks, first face with the glyph wins. The pack's
# *primary* font styles the common glyphs; a shared fallback tail catches exotic
# symbol / arrow / alchemical / CJK-description codepoints so obscure imported
# sets still render instead of dropping. This mirrors glyph-archive's web font
# stack (a styled primary + Noto Sans Symbols 2 + a CJK face), but freetype needs
# the files on disk — the OFL faces are bundled in fonts/ (run fetch_fonts.py).
# Missing paths are skipped, so on non-macOS hosts the bundled fonts still work.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def _font(name: str) -> str:
    return os.path.join(_FONT_DIR, name)


# Shared tail appended to the styled packs. Between them these cover the symbol,
# arrow, math and astral-alchemical ranges plus the CJK Ideographic Description
# characters — the exact blocks a curated glyph-archive export tends to pull in.
_GLYPH_FALLBACKS = [
    _font("NotoSansSymbols2-Regular.ttf"),                 # symbols / arrows (OFL, bundled)
    "/System/Library/Fonts/Apple Symbols.ttf",             # astral alchemical + broad symbols
    "/System/Library/Fonts/Supplemental/STIXTwoMath.otf",  # math / technical / astronomical
    "/System/Library/Fonts/Hiragino Sans GB.ttc",          # CJK Ideographic Description (U+2FFx)
]

_GLYPH_PACKS = {
    # native macOS look — broad symbol coverage out of the box
    "system": ["/System/Library/Fonts/Apple Symbols.ttf",
               "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
               "/System/Library/Fonts/Menlo.ttc", *_GLYPH_FALLBACKS],
    # plain grotesque
    "sans":   ["/System/Library/Fonts/Helvetica.ttc", *_GLYPH_FALLBACKS],
    # coding aesthetic (JetBrains Mono, bundled OFL)
    "mono":   [_font("JetBrainsMono-Regular.ttf"),
               "/System/Library/Fonts/Menlo.ttc", *_GLYPH_FALLBACKS],
    # editorial serif (Spectral, bundled OFL)
    "serif":  [_font("Spectral-Regular.ttf"), *_GLYPH_FALLBACKS],
    # GNU Unifont — unique blocky-pixel styling, near-total BMP coverage; the
    # "everything renders" pack (Apple Symbols/Noto fill the astral plane).
    "unifont": [_font("unifont.otf"),
                "/System/Library/Fonts/Apple Symbols.ttf",
                _font("NotoSansSymbols2-Regular.ttf")],
}

# Bundled from a glyph-archive export so `--glyph-palette favorites` works with
# no import. Missing-in-font entries are dropped automatically at analysis time.
_GLYPH_FAVORITES = (
    "⎐⁂⏧⍖↭↬⇶⇲⇱⇝⇜⊬"
    "⊹⋇⋱⋳⊶⊷⏦⎃⌖⌮⌭⎌"
    "⎄⌱⚵⚴⚳⚲⛼⛻☇☈☍☨"
    "⚆⚇⚈⚉⚞⚟⚷⚹⚺⚻⚼⛶"
    "⛯⛮⛭⛠⛚✢✣✤✥✧✱✲"
    "✶✷✸✹✺✻✼✽✿❀❁❅"
    "❈❉➟⯨⯩⯰⯲⯳⯴⯵⯶⯸"
    "⯻⯿⯢⯣⯤⯦⯧⯚⯙⯘⯖⯒"
    "⯐⯉⭞⭟⭜⭝⭗⭄⭃⬲⬳⬵"
    "⬴⬹⬺⬾\U0001f71f\U0001f71d\U0001f71a\U0001f725\U0001f726"
    "\U0001f727\U0001f72b\U0001f72c\U0001f72d\U0001f72f\U0001f732\U0001f733"
    "\U0001f737\U0001f738\U0001f73a\U0001f73c\U0001f73d\U0001f73f\U0001f740"
    "\U0001f744\U0001f745\U0001f746\U0001f74a\U0001f74b\U0001f74f\U0001f751"
    "\U0001f752\U0001f76e\U0001f76f\U0001f770\U0001f771\U0001f776\U0001f775"
    "\U0001f77c\U0001f77e\U0001f77f≗≛≬≭∺⫝̸⫝"
    "⨳⫱⥇⥺⥹⤳⥈⋕⊾∡∢⿲"
    "⿳⿴⿵⿶⿷⿸⿹⿺⿻⑂↹"
)

_GLYPH_PALETTES = {
    "ascii":   " .:-=+*#%@",                                # familiar density ramp
    "blocks":  " ·░▒▓█",           # shades — smooth tone steps
    "boxdraw": " ·─│╱╲╳┼├┤┬"
               "┴┌┐└┘═║╬",  # directional showcase
    "favorites": _GLYPH_FAVORITES,
}

# A glyph counts as "directional" (usable for an edge substitution) only when its
# own strokes are this coherent — a stroke, a slash, an arrow; not a busy symbol.
_GLYPH_DIR_COHERENCE = 0.35


def _clean_charset(text: str) -> List[str]:
    """Dedupe a character set, dropping whitespace, control and combining marks."""
    import unicodedata
    seen, out = set(), []
    for ch in text:
        if ch in seen or ch.isspace():
            continue
        if unicodedata.combining(ch) or unicodedata.category(ch).startswith("C"):
            continue
        seen.add(ch)
        out.append(ch)
    return out


def charset_from_glyph_json(data) -> str:
    """Extract a character set from a glyph-archive export (path, dict or list).

    Accepts the export shape ``{custom:[{cp}], favorites:[cp,...]}`` where each
    ``cp`` is a hex codepoint string, a bare list of hex codepoints, or already
    literal characters. Returns a plain string for ``glyph_chars``.
    """
    import json
    import os
    if isinstance(data, str):
        if data.lstrip().startswith(("{", "[")):
            data = json.loads(data)                         # raw JSON text
        elif os.path.exists(data):
            with open(data) as fh:
                data = json.load(fh)                        # filesystem path
        else:
            data = list(data)                               # treat as literal chars

    cps = []
    if isinstance(data, dict):
        for c in data.get("custom", []) or []:
            cp = c.get("cp") if isinstance(c, dict) else c
            if cp:
                cps.append(cp)
        cps += list(data.get("favorites", []) or [])
        if not cps:
            cps = list(data.get("codepoints", []) or [])
    elif isinstance(data, list):
        cps = data

    out = []
    for cp in cps:
        if isinstance(cp, str) and len(cp) == 1:
            out.append(cp)                                  # already a character
            continue
        try:
            out.append(chr(int(str(cp), 16)))
        except (ValueError, TypeError):
            continue
    return "".join(out)


def _glyph_faces(p: Params):
    """Resolve the selected font pack (or --glyph-font override) to freetype Faces.

    First face with a glyph for a codepoint wins, so a pack's primary font styles
    the common glyphs and the fallback tail catches the exotic ones. Missing paths
    are skipped (system fonts absent on non-macOS, un-fetched bundles).
    """
    import freetype
    if p.glyph_font:
        paths = [p.glyph_font]
    else:
        paths = _GLYPH_PACKS.get(p.glyph_pack, _GLYPH_PACKS["system"])
    faces = []
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        try:
            faces.append(freetype.Face(path))
        except Exception:                                   # unreadable/unsupported font
            continue
    if not faces:
        raise RuntimeError(
            "glyph mode: no usable font found — run `python fetch_fonts.py` to "
            "install the bundled packs, or pass --glyph-font a .ttf/.otf path"
        )
    return faces


def _glyph_charset(p: Params) -> List[str]:
    """The cleaned character set glyph mode will draw (explicit chars or palette)."""
    source = p.glyph_chars if p.glyph_chars.strip() else \
        _GLYPH_PALETTES.get(p.glyph_palette, _GLYPH_PALETTES["ascii"])
    return _clean_charset(source)


def glyph_coverage(p: Params):
    """(requested_count, [chars no font in the pack can draw]) for p's charset.

    A char is 'unsupported' when no face in the resolved pack has an outline for
    it — exactly the glyphs that would silently drop from the render. Cheap: only
    consults each face's cmap (no outline decode)."""
    faces = _glyph_faces(p)
    chars = _glyph_charset(p)
    missing = [ch for ch in chars
               if not any(f.get_char_index(ord(ch)) for f in faces)]
    return len(chars), missing


def _glyph_face(faces, ch: str):
    """First face in the stack with an outline for ``ch`` (else None)."""
    return next((f for f in faces if f.get_char_index(ord(ch)) != 0), None)


def _glyph_outline(face, ch: str, seg: int):
    """The font's native outline for ``ch`` on ``face``, flattened to polylines in
    normalized em space (y up, baseline at 0), plus the advance width in em.

    Shared by ``_analyze_glyph`` (tone ranking + the rendered grid) and the palette
    sheet's etched labels, so a glyph is drawn from the font's own vectors — no
    tracing — everywhere it appears.
    """
    import freetype
    upem = float(face.units_per_EM or 2048)
    face.load_char(ch, freetype.FT_LOAD_NO_SCALE | freetype.FT_LOAD_NO_BITMAP)
    adv = face.glyph.advance.x / upem
    polys: list = []

    def _pt(a):
        return (a.x / upem, a.y / upem)

    def move_to(a, _):
        polys.append([_pt(a)])

    def line_to(a, _):
        polys[-1].append(_pt(a))

    def conic_to(c, a, _):                                  # quadratic bezier
        (x0, y0), (cx, cy), (x1, y1) = polys[-1][-1], _pt(c), _pt(a)
        for i in range(1, seg + 1):
            t = i / seg
            mt = 1.0 - t
            polys[-1].append((mt * mt * x0 + 2 * mt * t * cx + t * t * x1,
                              mt * mt * y0 + 2 * mt * t * cy + t * t * y1))

    def cubic_to(c1, c2, a, _):                            # cubic bezier
        (x0, y0), (ax, ay), (bx, by), (x1, y1) = (polys[-1][-1], _pt(c1), _pt(c2), _pt(a))
        for i in range(1, seg + 1):
            t = i / seg
            mt = 1.0 - t
            polys[-1].append((
                mt**3 * x0 + 3 * mt * mt * t * ax + 3 * mt * t * t * bx + t**3 * x1,
                mt**3 * y0 + 3 * mt * mt * t * ay + 3 * mt * t * t * by + t**3 * y1))

    try:
        face.glyph.outline.decompose(None, move_to=move_to, line_to=line_to,
                                     conic_to=conic_to, cubic_to=cubic_to)
    except Exception:                                       # degenerate/empty outline
        pass
    polys = [np.asarray(pl, dtype=float) for pl in polys if len(pl) >= 2]
    return polys, adv


def _analyze_glyph(faces, ch: str, raster_px: int, seg: int):
    """Return a glyph record, or None if no font in the stack has this character.

    ``polys`` are the font's native outline contours (normalized em space, y up).
    ``cov`` is ink fraction of the em box and ``length`` the total outline stroke
    length in em units — the two tonal-rank metrics (``glyph_density``): coverage
    reads the filled area, outline the perforated line length (what a hairline
    laser cut actually darkens with). ``ang``/``coh`` are the dominant stroke
    orientation and coherence, used for edge-aware substitution.
    """
    face = _glyph_face(faces, ch)
    if face is None:
        return None

    # --- native outline (font's own vectors) -> normalized polylines ---
    polys, adv = _glyph_outline(face, ch, seg)
    length = float(sum(np.linalg.norm(np.diff(pl, axis=0), axis=1).sum()
                       for pl in polys))                    # total stroke length (em)

    # --- rasterize the same glyph for tone rank + stroke orientation ---
    import freetype
    face.set_pixel_sizes(0, raster_px)
    face.load_char(ch, freetype.FT_LOAD_RENDER)
    bm = face.glyph.bitmap
    cov = ang = coh = 0.0
    if bm.rows > 0 and bm.width > 0:
        buf = np.array(bm.buffer, dtype=np.uint8)
        arr = buf.reshape(bm.rows, bm.pitch)[:, :bm.width].astype(float)
        cov = float((arr > 0).sum()) / float(raster_px * raster_px)
        gy, gx = np.gradient(arr)
        sxx, syy, sxy = (gx * gx).sum(), (gy * gy).sum(), (gx * gy).sum()
        ang = float(0.5 * np.arctan2(2.0 * sxy, sxx - syy))
        denom = sxx + syy
        coh = float(np.hypot(sxx - syy, 2.0 * sxy) / denom) if denom > 1e-9 else 0.0

    return {"ch": ch, "polys": polys, "adv": adv, "cov": cov, "ang": ang,
            "coh": coh, "length": length}


def _glyph_density_key(metric: str):
    """The record field ranking glyphs light->dark for the chosen ``glyph_density``:
    ``outline`` = total stroke length (a hairline laser's perceived tone), else the
    rasterized ink ``cov``. One place so render + palette-sheet stay in lockstep."""
    field = "length" if metric == "outline" else "cov"
    return lambda r: r[field]


def _label_polylines(faces, text: str, height_mm: float, seg: int = 6):
    """Lay ``text`` out as hairline mm polylines from the font's own digit/letter
    outlines (same no-tracing rule as the glyphs), normalized so the drawn bounding
    box sits at the origin (y down). One em scales to ``height_mm``. Returns
    ``(polylines, width_mm, height_mm)`` — ``([], 0, 0)`` if nothing drew. Used for
    the palette sheet's etched rank labels."""
    x_em, raw = 0.0, []
    for ch in text:
        face = _glyph_face(faces, ch)
        if face is None:
            x_em += 0.5                                    # blank slot for a missing glyph
            continue
        polys, adv = _glyph_outline(face, ch, seg)
        for pl in polys:
            raw.append(pl + (x_em, 0.0))                   # shift right in em space
        x_em += adv if adv > 0 else 0.5
    if not raw:
        return [], 0.0, 0.0
    scaled = [np.column_stack([pl[:, 0] * height_mm, -pl[:, 1] * height_mm])
              for pl in raw]                               # em -> mm, flip y down
    mn = np.vstack(scaled).min(axis=0)
    w, h = np.vstack(scaled).max(axis=0) - mn
    return [s - mn for s in scaled], float(w), float(h)    # top-left -> origin


class GlyphInstances:
    """Instanced glyph output: distinct glyph outlines defined once, placed many times.

    ``glyphs`` maps a glyph id to its list of local-origin polylines (mm, y down).
    ``placements`` is a list of ``(glyph_id, tx, ty)`` cell centers. Serialized to
    ``<defs>`` + one ``<use transform="translate(tx ty)">`` per placement — much
    smaller than repeating the outline per cell, and importable by Affinity (which
    ignores x/y attributes on <use> but honors translate). Only ``render_glyph``
    with ``--glyph-instance`` returns this; every other mode returns flat polylines.
    """
    __slots__ = ("glyphs", "placements")

    def __init__(self, glyphs, placements):
        self.glyphs = glyphs
        self.placements = placements


def render_glyph(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]:
    """ASCII / Unicode line art — a grid of glyphs whose ink density tracks tone.

    Each glyph is analyzed once: the font's native outline (drawn as hairlines),
    its ink coverage (tonal rank), and its dominant stroke orientation. The image
    is pooled to a cell grid; every cell picks the glyph whose coverage matches
    the local darkness. With ``--glyph-edge`` on, cells sitting on a coherent edge
    instead pick the directional glyph whose stroke best aligns with that edge,
    blended against the tonal match by the edge strength.
    """
    try:
        import freetype  # noqa: F401
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError("glyph mode needs freetype-py; pip install freetype-py") from exc
    from skimage.filters import gaussian
    from skimage.transform import resize

    faces = _glyph_faces(p)
    chars = _glyph_charset(p)

    recs = [r for r in (_analyze_glyph(faces, ch, 64, 8) for ch in chars)
            if r is not None and r["polys"]]
    if not recs:
        return []

    # Ramp sorted light->dark by the chosen density metric (coverage | outline);
    # index 0 is a synthetic BLANK so highlights stay empty.
    dkey = _glyph_density_key(p.glyph_density)
    recs.sort(key=dkey)
    dens = np.array([0.0] + [dkey(r) for r in recs])
    dens_n = dens / (dens.max() or 1.0)                    # normalized 0..1
    ang = np.array([0.0] + [r["ang"] for r in recs])
    coh = np.array([0.0] + [r["coh"] for r in recs])
    dir_mask = coh >= _GLYPH_DIR_COHERENCE
    polys_by_glyph = [None] + [r["polys"] for r in recs]
    adv_by_glyph = np.array([0.0] + [r["adv"] for r in recs])
    G = len(dens_n)

    # Cell grid: columns fixed, rows derived so cells honor --glyph-aspect.
    cols = max(1, int(p.glyph_cols))
    cw = width_mm / cols
    ch_mm = cw * max(0.05, p.glyph_aspect)
    rows = max(1, int(round(height_mm / ch_mm)))
    ch_mm = height_mm / rows

    bright = np.clip(resize(gray, (rows, cols), order=1, anti_aliasing=True,
                            preserve_range=True), 0.0, 1.0)
    target = (1.0 - bright) ** max(1e-6, p.glyph_gamma)     # per-cell darkness (rows,cols)

    lam = float(p.glyph_edge)
    if lam > 0.0:
        # Pooled structure tensor -> per-cell dominant edge orientation + coherence.
        g = gaussian(gray, sigma=1.5, preserve_range=True)
        gy, gx = np.gradient(g)
        sig = max(1.0, min(gray.shape) / max(rows, cols) * 0.5)
        pool = lambda a: resize(gaussian(a, sigma=sig, preserve_range=True),
                                (rows, cols), order=1, anti_aliasing=True,
                                preserve_range=True)
        sxx, syy, sxy = pool(gx * gx), pool(gy * gy), pool(gx * gy)
        theta_img = 0.5 * np.arctan2(2.0 * sxy, sxx - syy)
        denom = sxx + syy
        coh_img = np.where(denom > 1e-12, np.hypot(sxx - syy, 2.0 * sxy) /
                           np.maximum(denom, 1e-12), 0.0)

        tone = np.abs(dens_n[None, :] - target.ravel()[:, None])    # (M,G)
        dth = ang[None, :] - theta_img.ravel()[:, None]
        adist = np.abs(((dth + np.pi / 2) % np.pi) - np.pi / 2) / (np.pi / 2)  # 0..1
        dirpen = np.where(dir_mask[None, :], adist, 1.0)   # non-directional glyphs disfavored
        lam_cell = (lam * (coh_img.ravel() >= p.glyph_edge_threshold))[:, None]
        choice = np.argmin((1.0 - lam_cell) * tone + lam_cell * dirpen,
                           axis=1).reshape(rows, cols)
    else:
        # Pure density: nearest step on the sorted ramp (no big score matrix).
        tgt = target.ravel()
        idx = np.clip(np.searchsorted(dens_n, tgt), 1, G - 1)
        pick_low = (tgt - dens_n[idx - 1]) <= (dens_n[idx] - tgt)
        choice = np.where(pick_low, idx - 1, idx).reshape(rows, cols)

    mask = (bright > p.mask_threshold) if p.mask_threshold is not None else None

    s_em = p.glyph_size * min(cw, ch_mm)                    # 1 em -> this many mm
    y_center = 0.35                                         # em fraction ~ visual middle

    def _cell_center(r, c):
        return c * cw + cw / 2.0, r * ch_mm + ch_mm / 2.0

    def _placed(gi):                                        # glyph gi's outlines at local origin (y down, mm)
        x_center = adv_by_glyph[gi] / 2.0 if adv_by_glyph[gi] > 0 else 0.25
        segs = []
        for pl in polys_by_glyph[gi]:
            seg = np.column_stack([(pl[:, 0] - x_center) * s_em,
                                   -(pl[:, 1] - y_center) * s_em])
            seg = rdp(seg, p.decimate)                      # RDP is translation-invariant
            if len(seg) >= 2:
                segs.append(seg)
        return segs

    if p.glyph_instance:
        # Instanced output: define each distinct glyph once, place with one
        # <use transform="translate(...)"> per cell. Affinity Designer's importer
        # ignores x/y attributes on <use> (matplotlib #20910) but honors translate,
        # so this stays importable while collapsing thousands of outline copies.
        local = {}
        placements = []
        for r in range(rows):
            for c in range(cols):
                gi = int(choice[r, c])
                if gi == 0 or (mask is not None and mask[r, c]):
                    continue                                # blank cell / masked highlight
                segs = local.get(gi)
                if segs is None:
                    segs = local[gi] = _placed(gi)
                if not segs:
                    continue
                cx, cy = _cell_center(r, c)
                placements.append((gi, cx, cy))
        return GlyphInstances(local, placements)

    out = []
    for r in range(rows):
        for c in range(cols):
            gi = int(choice[r, c])
            if gi == 0 or (mask is not None and mask[r, c]):
                continue                                    # blank cell / masked highlight
            cx, cy = _cell_center(r, c)
            for seg in _placed(gi):
                out.append(seg + (cx, cy))                  # translate local outline to the cell
    return out


_MODES = {
    "wavy": render_wavy,
    "spacing": render_spacing,
    "contour": render_contour,
    "filet": render_filet,
    "flow": render_flow,
    "tsp": render_tsp,
    "glyph": render_glyph,
}


# --------------------------------------------------------------------------- #
# SVG serialization
# --------------------------------------------------------------------------- #
_COORD_DECIMALS = 2                                  # path grid: 0.01mm (< laser resolution)
_Q = 10 ** _COORD_DECIMALS


def _fmt(v):  # compact fixed-point; strips trailing zeros (viewBox / header dims)
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _num(iv):  # integer grid units -> compact decimal string
    sign = "-" if iv < 0 else ""
    whole, frac = divmod(abs(iv), _Q)
    if frac == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{f'{frac:0{_COORD_DECIMALS}d}'.rstrip('0')}"


def _pair(a, b):  # "a b", dropping the separator when b is self-delimiting ('-')
    sb = _num(b)
    return _num(a) + ("" if sb[0] == "-" else " ") + sb


def _path_d(pl):
    """Relative-move path data for one mm-space polyline (see polylines_to_svg)."""
    pts = [(int(round(x * _Q)), int(round(y * _Q))) for x, y in pl]
    px, py = pts[0]
    d = "M" + _pair(px, py)
    for x, y in pts[1:]:
        d += "l" + _pair(x - px, y - py)
        px, py = x, y
    return d


def _svg_document(body, width_mm, height_mm, p: Params, extra_svg_attr=""):
    """Wrap a serialized body in the hairline SVG shell (shared header)."""
    # Coordinate space (paths + viewBox) is always mm; the physical size in the
    # header is the same length expressed in the requested unit. viewBox units need
    # not equal header units — the header sets true physical size, the viewBox is
    # just the internal grid — so the file imports at correct scale either way.
    per = _MM_PER_UNIT.get(p.units, 1.0)
    w, h = _fmt(width_mm), _fmt(height_mm)               # viewBox / path units (mm)
    dw, dh = _fmt(width_mm / per), _fmt(height_mm / per)  # header, in display unit
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1"{extra_svg_attr}\n'
        f'     width="{dw}{p.units}" height="{dh}{p.units}" viewBox="0 0 {w} {h}">\n'
        f'  <g class="ink" fill="none" stroke="{p.color}" stroke-width="{p.stroke_width}"\n'
        f'     stroke-linecap="round" stroke-linejoin="round">\n'
        f"{body}\n"
        f"  </g>\n"
        f"</svg>\n"
    )


def polylines_to_svg(polylines, width_mm, height_mm, p: Params) -> str:
    """Serialize mm-space polylines to a hairline SVG string.

    Path data uses *relative* moves on a fixed integer grid (``_COORD_DECIMALS``
    places). Consecutive polyline vertices sit microns apart, so their deltas are
    tiny numbers that cost far fewer characters than absolute coordinates — a
    ~40% smaller file, most visible in the glyph mode's dense outlines. Quantizing
    each point to the grid *before* differencing means the deltas are exact
    integer differences, so rounding can never accumulate along a path (important
    for the tsp mode's single 10k-point tour).
    """
    paths = [f'<path d="{_path_d(pl)}"/>' for pl in polylines if len(pl) >= 2]
    return _svg_document("\n".join(paths), width_mm, height_mm, p)


def glyph_instances_to_svg(inst: "GlyphInstances", width_mm, height_mm, p: Params) -> str:
    """Serialize instanced glyph output: each glyph defined once, placed with <use>.

    Only the glyphs actually placed are defined. Each cell becomes a single
    ``<use xlink:href="#gN" transform="translate(tx ty)">`` — Affinity Designer's
    importer ignores x/y attributes on <use> (matplotlib #20910) but honors
    translate, which is also what the merged matplotlib fix (PR #28504) switched
    to. Collapsing thousands of repeated outlines into ~one def per distinct glyph
    is a large size win on dense grids.
    """
    used = sorted({gi for gi, _, _ in inst.placements})
    defs = []
    for gi in used:
        inner = "".join(f'<path d="{_path_d(seg)}"/>' for seg in inst.glyphs[gi])
        defs.append(f'<g id="g{gi}">{inner}</g>')
    uses = []
    for gi, tx, ty in inst.placements:
        t = f"{_num(int(round(tx * _Q)))} {_num(int(round(ty * _Q)))}"
        uses.append(f'<use xlink:href="#g{gi}" transform="translate({t})"/>')
    body = "  <defs>\n    " + "\n    ".join(defs) + "\n  </defs>\n" + "\n".join(uses)
    return _svg_document(body, width_mm, height_mm, p,
                         extra_svg_attr=' xmlns:xlink="http://www.w3.org/1999/xlink"')


def glyph_palette_svg(p: Params):
    """Render the selected glyph charset as a sorted density-ramp reference sheet.

    A laser-cuttable swatch: each cell draws a glyph's native hairline outline over
    an etched rank index (1 = lightest), laid out light->dark by ``p.glyph_density``
    — ``coverage`` (rasterized ink area) or ``outline`` (total stroke length ~=
    microperforation count, the tone you actually perceive once the hairlines are
    cut). Uses the SAME sort as ``render_glyph``, so the sheet is a faithful legend
    for a portrait rendered with the same params. Works for a built-in palette, an
    explicit ``glyph_chars`` set, or a pre-processed imported glyph-archive JSON —
    whatever ``_glyph_charset`` resolves. Takes no input image; returns (svg, stats).
    """
    try:
        import freetype  # noqa: F401
    except ImportError as exc:                              # pragma: no cover
        raise RuntimeError("glyph mode needs freetype-py; pip install freetype-py") from exc

    faces = _glyph_faces(p)
    chars = _glyph_charset(p)
    recs = [r for r in (_analyze_glyph(faces, ch, 64, 8) for ch in chars)
            if r is not None and r["polys"]]
    metric = "outline" if p.glyph_density == "outline" else "coverage"
    recs.sort(key=_glyph_density_key(metric))              # light -> dark, same as render

    cell = max(1.0, float(p.glyph_palette_cell))           # mm per glyph cell
    n = len(recs)
    cols = max(1, min(int(p.glyph_palette_cols), n or 1))
    rows = max(1, (n + cols - 1) // cols)
    gap = 0.10 * cell                                      # glyph square -> label band
    label_h = 0.20 * cell                                  # etched index cap height (mm)
    cell_h = cell + gap + label_h
    width_mm, height_mm = cols * cell, rows * cell_h

    s_em = max(0.05, min(p.glyph_size, 1.0)) * cell        # glyph 1em -> mm
    y_center = 0.35                                        # em fraction ~ visual middle
    out = []
    for i, r in enumerate(recs):
        cr, cc = divmod(i, cols)
        cx, cy = cc * cell + cell / 2.0, cr * cell_h + cell / 2.0
        x_center = r["adv"] / 2.0 if r["adv"] > 0 else 0.25
        for pl in r["polys"]:                              # the glyph, centered in its square
            seg = np.column_stack([(pl[:, 0] - x_center) * s_em,
                                   -(pl[:, 1] - y_center) * s_em])
            seg = rdp(seg, p.decimate)                     # RDP is translation-invariant
            if len(seg) >= 2:
                out.append(seg + (cx, cy))
        lab, lw, _ = _label_polylines(faces, str(i + 1), label_h)   # etched rank
        lx, ly = cc * cell + (cell - lw) / 2.0, cr * cell_h + cell + gap
        for seg in lab:
            out.append(seg + (lx, ly))

    svg = polylines_to_svg(out, width_mm, height_mm, p)
    _, missing = glyph_coverage(p)
    per = _MM_PER_UNIT.get(p.units, 1.0)
    stats = {
        "mode": "glyph-palette", "density": metric,
        "glyph_pack": "custom" if p.glyph_font else p.glyph_pack,
        "cols": cols, "rows": rows, "glyphs": n,
        "glyphs_requested": len(chars), "glyphs_missing": len(missing),
        "width_mm": round(width_mm, 3), "height_mm": round(height_mm, 3),
        "units": p.units,
        "width_disp": round(width_mm / per, 4), "height_disp": round(height_mm / per, 4),
        "paths": sum(1 for pl in out if len(pl) >= 2),
        "points": int(sum(len(pl) for pl in out)),
    }
    return svg, stats


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

    result = _MODES[p.mode](gray, p, width_mm, height_mm)

    if isinstance(result, GlyphInstances):
        svg = glyph_instances_to_svg(result, width_mm, height_mm, p)
        n_paths = len(result.placements)                 # one <use> per placed cell
        n_points = int(sum(sum(len(seg) for seg in result.glyphs[gi])
                           for gi, _, _ in result.placements))
    else:
        svg = polylines_to_svg(result, width_mm, height_mm, p)
        n_paths = len(result)
        n_points = int(sum(len(pl) for pl in result))

    per = _MM_PER_UNIT.get(p.units, 1.0)
    stats = {
        "mode": p.mode,
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "units": p.units,
        "width_disp": round(width_mm / per, 4),
        "height_disp": round(height_mm / per, 4),
        "paths": n_paths,
        "points": n_points,
    }
    if p.mode == "glyph":
        # Report how many of the requested glyphs the selected pack can actually
        # draw, so the UI can warn about the rest (silently dropped otherwise).
        try:
            requested, missing = glyph_coverage(p)
            stats["glyph_pack"] = "custom" if p.glyph_font else p.glyph_pack
            stats["glyphs_requested"] = requested
            stats["glyphs_missing"] = len(missing)
        except Exception:                                    # freetype absent, etc.
            pass
    return svg, stats


# --------------------------------------------------------------------------- #
# Output naming — shared by every CLI and web server in the family so the same
# input yields `<stem>_<tag>.svg` everywhere (tag = mode / "mask" / "cut").
# --------------------------------------------------------------------------- #
def safe_stem(filename: str, fallback: str = "output") -> str:
    """Filesystem-safe stem of a path/filename: no directory, no extension,
    unsafe characters collapsed to '_'. Empty result falls back to `fallback`."""
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem or fallback


def default_output_path(input_path: str, tag: str) -> str:
    """`<stem>_<tag>.svg` next to the input — the CLI's output when -o is
    omitted. Callers pass ``-o -`` to force stdout instead."""
    name = f"{safe_stem(input_path, 'output')}_{tag}.svg"
    d = os.path.dirname(input_path)
    return os.path.join(d, name) if d else name


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
    ap.add_argument("input", nargs="?",
                    help="input raster image (PNG/JPG/...); optional only when "
                         "--glyph-palette-export is given")
    ap.add_argument("-o", "--output",
                    help="output SVG path (default: <input>_<mode>.svg next to "
                         "the input; pass '-' for stdout)")
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
    w.add_argument("--amp-gamma", type=float, default=d.amp_gamma,
                   help="amplitude response curve; <1 lifts midtones (more contrast)")
    w.add_argument("--phase-jitter", type=float, default=d.phase_jitter,
                   help="per-line phase decorrelation 0..1 (breaks vertical banding)")
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
    s.add_argument("--spacing-style", choices=["clean", "density"],
                   default=d.spacing_style,
                   help="'clean' = crisp lines clipped to form; "
                        "'density' = per-column shading (detailed but dithery)")

    c = ap.add_argument_group("contour")
    c.add_argument("--levels", type=int, default=d.levels,
                   help="brightness quantization bands")
    c.add_argument("--smooth", type=float, default=d.smooth,
                   help="Gaussian blur sigma applied pre-contour (px)")
    c.add_argument("--min-contour-len", type=float, default=d.min_contour_len,
                   help="drop contours shorter than this (mm)")
    c.add_argument("--smooth-mode", choices=["gaussian", "bilateral"],
                   default=d.smooth_mode,
                   help="pre-contour blur: 'gaussian' or edge-preserving 'bilateral'")
    c.add_argument("--bilateral-color", type=float, default=d.bilateral_color,
                   help="bilateral tonal sigma (0..1); smaller = harder edges")
    c.add_argument("--contour-source", choices=["tone", "edge"],
                   default=d.contour_source,
                   help="trace 'tone' (brightness) or 'edge' (gradient magnitude)")
    c.add_argument("--min-contour-area", type=float, default=d.min_contour_area,
                   help="drop closed loops enclosing less than this (mm^2); 0 = off")
    c.add_argument("--max-contour-len", type=float, default=d.max_contour_len,
                   help="drop contours longer than this (mm); 0 = off")
    c.add_argument("--islands-only", action=argparse.BooleanOptionalAction,
                   default=d.islands_only,
                   help="keep only closed loops (drop open/border contours)")

    f = ap.add_argument_group("filet")
    f.add_argument("--cells-wide", type=int, default=d.cells_wide,
                   help="grid columns; rows derived to keep cells square")
    f.add_argument("--fill-threshold", type=float, default=d.fill_threshold,
                   help="cell is 'filled' when darkness >= this (0..1)")
    f.add_argument("--fill-style", choices=["x", "cross", "hatch"],
                   default=d.fill_style,
                   help="filled-cell mark: 'x' | 'cross' | 'hatch'")
    f.add_argument("--hatch-lines", type=int, default=d.hatch_lines,
                   help="parallel diagonals per cell for --fill-style hatch")
    f.add_argument("--mesh", action=argparse.BooleanOptionalAction, default=d.mesh,
                   help="draw the full grid lattice (--no-mesh: filled cells only)")

    fl = ap.add_argument_group("flow")
    fl.add_argument("--flow-smooth", type=float, default=d.flow_smooth,
                    help="structure-tensor blur sigma in px (field coherence)")
    fl.add_argument("--flow-spacing", type=float, default=d.flow_spacing,
                    help="mm between seed points (grid pitch)")
    fl.add_argument("--flow-len", type=float, default=d.flow_len,
                    help="mm arc length of each streamline stroke")
    fl.add_argument("--flow-step", type=float, default=d.flow_step,
                    help="mm integration step along a streamline")
    fl.add_argument("--flow-gamma", type=float, default=d.flow_gamma,
                    help="seed-density response curve; <1 lifts midtones")

    t = ap.add_argument_group("tsp")
    t.add_argument("--points", type=int, default=d.points,
                   help="target stipple dot count (tour vertices)")
    t.add_argument("--point-gamma", type=float, default=d.point_gamma,
                   help="darkness weighting for dot density; <1 lifts midtones")
    t.add_argument("--tsp-improve", type=int, default=d.tsp_improve,
                   help="interleaved 2-opt + or-opt refinement passes (0 = raw Hilbert seed)")

    gl = ap.add_argument_group("glyph")
    gl.add_argument("--glyph-cols", type=int, default=d.glyph_cols,
                    help="grid columns (rows derived from --glyph-aspect)")
    gl.add_argument("--glyph-palette", choices=list(_GLYPH_PALETTES), default=d.glyph_palette,
                    help="built-in character ramp")
    gl.add_argument("--glyph-chars", default=d.glyph_chars,
                    help="explicit character set, overrides --glyph-palette")
    gl.add_argument("--glyph-json",
                    help="load a glyph-archive export (JSON) as the character set")
    gl.add_argument("--glyph-pack", choices=list(_GLYPH_PACKS), default=d.glyph_pack,
                    help="font pack (styles the glyphs); run fetch_fonts.py first")
    gl.add_argument("--glyph-font", default=d.glyph_font,
                    help="single font file (.ttf/.otf) override; wins over --glyph-pack")
    gl.add_argument("--glyph-gamma", type=float, default=d.glyph_gamma,
                    help="darkness response curve; <1 lifts midtones")
    gl.add_argument("--glyph-size", type=float, default=d.glyph_size,
                    help="glyph size as a fraction of the cell (0..1)")
    gl.add_argument("--glyph-aspect", type=float, default=d.glyph_aspect,
                    help="cell height/width ratio (1 = square)")
    gl.add_argument("--glyph-edge", type=float, default=d.glyph_edge,
                    help="edge-direction awareness 0..1 (0 = pure density)")
    gl.add_argument("--glyph-edge-threshold", type=float, default=d.glyph_edge_threshold,
                    help="min local edge coherence to allow a directional glyph swap")
    gl.add_argument("--glyph-instance", action="store_true", default=d.glyph_instance,
                    help="define each glyph once in <defs>, place with <use> "
                         "(smaller file; verify your SVG importer handles <use>)")
    gl.add_argument("--glyph-density", choices=["coverage", "outline"],
                    default=d.glyph_density,
                    help="tonal-rank metric: 'coverage' (rasterized ink area) or "
                         "'outline' (total stroke length ~ microperforation count, "
                         "the tone a hairline laser cut actually reads)")
    gl.add_argument("--glyph-palette-export", metavar="PATH",
                    help="write a sorted density-ramp reference sheet of the glyph "
                         "charset (glyphs + etched rank labels) to PATH and exit; "
                         "needs no input image, '-' for stdout. Honors --glyph-"
                         "palette/-chars/-json/-pack/-font/-density")
    gl.add_argument("--glyph-palette-cols", type=int, default=d.glyph_palette_cols,
                    help="cells per row on the exported palette sheet")
    gl.add_argument("--glyph-palette-cell", type=float, default=d.glyph_palette_cell,
                    help="mm per glyph cell on the exported palette sheet")
    return ap


def params_from_args(ns) -> Params:
    kwargs = {}
    for f in fields(Params):
        if hasattr(ns, f.name):
            kwargs[f.name] = getattr(ns, f.name)
    return Params(**kwargs)


def main(argv=None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if getattr(ns, "glyph_json", None):                     # feed a glyph-archive export in
        try:
            ns.glyph_chars = charset_from_glyph_json(ns.glyph_json)
        except (OSError, ValueError) as exc:
            print(f"error: couldn't read --glyph-json: {exc}", file=sys.stderr)
            return 1
    p = params_from_args(ns)

    # Palette-sheet export: a density-ordered legend for the charset, no input image.
    if ns.glyph_palette_export:
        try:
            svg, stats = glyph_palette_svg(p)
        except RuntimeError as exc:                          # freetype/fonts absent
            print(f"error: {exc}", file=sys.stderr)
            return 1
        out = ns.glyph_palette_export
        if out == "-":
            sys.stdout.write(svg)
        else:
            with open(out, "w") as fh:
                fh.write(svg)
            print(f"wrote {out}  [glyph-palette · {stats['density']}]  "
                  f"{stats['glyphs']} glyphs  {stats['cols']}x{stats['rows']} cells  "
                  f"{stats['width_mm']}x{stats['height_mm']}mm", file=sys.stderr)
        if stats.get("glyphs_missing"):
            print(f"warning: {stats['glyphs_missing']} of {stats['glyphs_requested']} "
                  f"glyphs have no outline in the '{stats['glyph_pack']}' font pack "
                  f"and were skipped; try --glyph-pack unifont", file=sys.stderr)
        return 0

    if not ns.input:
        parser.error("input image is required (or use --glyph-palette-export)")
    try:
        svg, stats = image_to_svg(ns.input, p)
    except FileNotFoundError:
        print(f"error: input not found: {ns.input}", file=sys.stderr)
        return 1

    out = ns.output or default_output_path(ns.input, p.mode)
    if out == "-":
        sys.stdout.write(svg)
    else:
        with open(out, "w") as fh:
            fh.write(svg)
        print(
            f"wrote {out}  [{stats['mode']}]  "
            f"{stats['width_mm']}x{stats['height_mm']}mm  "
            f"{stats['paths']} paths / {stats['points']} pts",
            file=sys.stderr,
        )
    miss = stats.get("glyphs_missing", 0)
    if miss:
        pack = stats.get("glyph_pack", p.glyph_pack)
        print(f"warning: {miss} of {stats['glyphs_requested']} glyphs have no "
              f"outline in the '{pack}' font pack and were skipped; try "
              f"--glyph-pack unifont for the widest coverage", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
