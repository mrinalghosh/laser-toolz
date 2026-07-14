#!/usr/bin/env python3
"""
epilogue.py — make any SVG "Epilog-safe": flatten transforms, fix true scale.

This is a third sibling to linify.py / segment.py. Where those two *generate*
SVG from a raster image, epilogue *ingests* an existing SVG (from Inkscape,
Affinity Designer, Illustrator, or linify/segment themselves) and rewrites it
into the one dialect the Epilog print driver reliably imports. It is the
"epilogue" that runs after your art tool — the last pass before the laser.

It fixes the two most common Epilog-driver failures, both purely geometric:

  1. WRONG SCALE. Inkscape assumes 96 (older: 90) DPI while the driver reads
     72, so files land 25-33% too big; Affinity 2 exports at an arbitrary
     scale. epilogue reads the document's declared physical size (or a
     --width-mm you force) and re-emits geometry in real millimetres with a
     matching viewBox, so it imports at exactly the size you asked for — the
     same true-scale header linify/segment already use.

  2. UNRENDERED TRANSFORMS. The Epilog driver silently drops any <path> that
     sits inside a `transform` (a group translate/scale, a matrix, a <use>).
     epilogue composes every transform down the tree and BAKES it into the
     coordinates, then emits flat, transform-free <path>s.

The flatten and the rescale are one matrix pass: the root CTM maps the input's
user units straight to millimetres, so every baked point lands in mm space.
Basic shapes (rect/circle/ellipse/line/poly*) and arcs are converted to
paths/cubics; <use> is resolved against the document's ids; quadratics are
elevated to cubics. Output reuses linify's compact 0.01 mm relative-grid
encoding.

Not handled (warned about, then skipped/passed through): <text> (convert to
paths in your editor first), raster <image>, and CSS class styling in a
<style> block (inline `style=` and presentation attributes ARE read).

Usage:
  python epilogue.py in.svg -o out.svg                 # flatten + true scale
  python epilogue.py in.svg -o out.svg --width-mm 200  # force physical width
  python epilogue.py messy.svg -o out.svg --dpi 90     # old-Inkscape px files
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional

# Reuse linify's mm-grid encoders so all three tools speak one coordinate
# language: relative moves on a 0.01 mm integer grid, true-scale unit header.
from linify import _MM_PER_UNIT, _fmt, _num, _Q, default_output_path

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"

# Length units -> millimetres. px depends on DPI (resolved at parse time).
_LEN_TO_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0,
              "pc": 25.4 / 6.0, "q": 0.25}
_LEN_RE = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*"
                     r"(px|pt|pc|mm|cm|in|q|%)?\s*$", re.I)


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass
class EpiParams:
    """All tunables in one place (mirrors linify.Params' single-source pattern)."""

    # --- scale / units ---
    width_mm: Optional[float] = None  # force physical width (mm); None = read from file
    units: str = "mm"                 # header unit: mm | cm | in
    dpi: float = 96.0                 # px->mm assumption for unitless / px lengths

    # --- hairline normalization ---
    hairline: bool = True             # force every path to fill:none + hairline stroke
    stroke_width: str = "0.02"        # hairline width in mm (the laser ignores it anyway)
    color: str = "#000000"            # single cut-stroke colour when hairline-normalizing

    # --- colour audit / operation mapping ---
    snap_colors: bool = False         # snap each op colour to a clean, exact primary


# --------------------------------------------------------------------------- #
# Affine matrices  (a, b, c, d, e, f)  ->  x' = a·x + c·y + e , y' = b·x + d·y + f
# --------------------------------------------------------------------------- #
_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(A, B):
    """Matrix that applies B then A (A∘B) — parent · child composition order."""
    a1, b1, c1, d1, e1, f1 = A
    a2, b2, c2, d2, e2, f2 = B
    return (a1 * a2 + c1 * b2,
            b1 * a2 + d1 * b2,
            a1 * c2 + c1 * d2,
            b1 * c2 + d1 * d2,
            a1 * e2 + c1 * f2 + e1,
            b1 * e2 + d1 * f2 + f1)


def _apply(M, x, y):
    a, b, c, d, e, f = M
    return (a * x + c * y + e, b * x + d * y + f)


def _avg_scale(M):
    """Geometric-mean scale of the matrix (for scaling stroke widths to mm)."""
    a, b, c, d, _, _ = M
    return math.sqrt(abs(a * d - b * c)) or 1.0


_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")


def _parse_transform(s: Optional[str]):
    """Parse an SVG transform attribute into one composed affine matrix."""
    M = _IDENT
    if not s:
        return M
    for name, arg in _TRANSFORM_RE.findall(s):
        nums = [float(v) for v in _NUM_RE.findall(arg)]  # findall, not split:
        if name == "matrix" and len(nums) == 6:
            T = tuple(nums)
        elif name == "translate":
            tx = nums[0] if nums else 0.0
            ty = nums[1] if len(nums) > 1 else 0.0
            T = (1, 0, 0, 1, tx, ty)
        elif name == "scale":
            sx = nums[0] if nums else 1.0
            sy = nums[1] if len(nums) > 1 else sx
            T = (sx, 0, 0, sy, 0, 0)
        elif name == "rotate":
            ang = math.radians(nums[0]) if nums else 0.0
            cos, sin = math.cos(ang), math.sin(ang)
            R = (cos, sin, -sin, cos, 0, 0)
            if len(nums) >= 3:  # rotate(a cx cy) = translate(c)·rotate·translate(-c)
                cx, cy = nums[1], nums[2]
                R = _mul((1, 0, 0, 1, cx, cy), _mul(R, (1, 0, 0, 1, -cx, -cy)))
            T = R
        elif name == "skewX":
            T = (1, 0, math.tan(math.radians(nums[0])) if nums else 0, 1, 0, 0)
        elif name == "skewY":
            T = (1, math.tan(math.radians(nums[0])) if nums else 0, 0, 1, 0, 0)
        else:
            T = _IDENT
        M = _mul(M, T)
    return M


# --------------------------------------------------------------------------- #
# Path data → absolute subpaths of line / cubic segments
# --------------------------------------------------------------------------- #
# A subpath is {"start": (x, y), "segs": [seg, ...], "closed": bool} where each
# seg is ("L", x, y) or ("C", x1, y1, x2, y2, x, y). Everything is reduced to
# these two so the emitter and the affine bake stay trivial (both are
# affine-invariant: transform the control points and you're done).
_NUM_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")


def _quad_to_cubic(x0, y0, qx, qy, x, y):
    """Elevate a quadratic Bézier to a cubic (exact)."""
    return ("C", x0 + 2 / 3 * (qx - x0), y0 + 2 / 3 * (qy - y0),
            x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), x, y)


def _arc_to_cubics(x0, y0, rx, ry, phi_deg, large, sweep, x, y):
    """Convert an SVG elliptical arc to a list of cubic Bézier segments."""
    if rx == 0 or ry == 0 or (x0 == x and y0 == y):
        return [("L", x, y)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg % 360.0)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x) / 2.0, (y0 - y) / 2.0
    x1p = cosp * dx + sinp * dy
    y1p = -sinp * dx + cosp * dy
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (x0 + x) / 2.0
    cy = sinp * cxp + cosp * cyp + (y0 + y) / 2.0

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / d)) if d else 1.0
        a = math.acos(c)
        return -a if (ux * vy - uy * vx) < 0 else a

    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta1 = ang(1, 0, ux, uy)
    dtheta = ang(ux, uy, vx, vy)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi

    n = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / n
    t = (8.0 / 3.0) * math.sin(delta / 4.0) ** 2 / math.sin(delta / 2.0) \
        if math.sin(delta / 2.0) else 0.0
    segs = []
    th = theta1
    px, py = x0, y0
    for i in range(n):
        th2 = th + delta
        cos2, sin2 = math.cos(th2), math.sin(th2)
        ex = cosp * rx * cos2 - sinp * ry * sin2 + cx
        ey = sinp * rx * cos2 + cosp * ry * sin2 + cy
        d1x, d1y = -rx * math.sin(th), ry * math.cos(th)
        d2x, d2y = -rx * sin2, ry * cos2
        c1x = px + t * (cosp * d1x - sinp * d1y)
        c1y = py + t * (sinp * d1x + cosp * d1y)
        c2x = ex - t * (cosp * d2x - sinp * d2y)
        c2y = ey - t * (sinp * d2x + cosp * d2y)
        segs.append(("C", c1x, c1y, c2x, c2y, ex, ey))
        px, py, th = ex, ey, th2
    return segs


def _parse_path_d(d: str):
    """Parse a path `d` string into absolute line/cubic subpaths."""
    subs: List[dict] = []
    cur = None                      # open subpath dict
    cx = cy = sx = sy = 0.0         # current point, subpath start
    pcx = pcy = None                # previous cubic control (for S)
    pqx = pqy = None                # previous quad control (for T)
    i, n = 0, len(d)
    cmd = None

    def start_sub(x, y):
        nonlocal cur
        cur = {"start": (x, y), "segs": [], "closed": False}
        subs.append(cur)

    while i < n:
        while i < n and d[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if d[i].isalpha():
            cmd = d[i]
            i += 1
        if cmd is None:                 # data must open with a command letter
            break
        rel = cmd.islower()
        C = cmd.upper()

        def read_num():
            nonlocal i
            while i < len(d) and d[i] in " \t\r\n,":
                i += 1
            m = _NUM_RE.match(d, i)
            if not m:
                return None
            i = m.end()
            return float(m.group())

        def read_flag():
            nonlocal i
            while i < len(d) and d[i] in " \t\r\n,":
                i += 1
            if i < len(d) and d[i] in "01":
                v = int(d[i]); i += 1
                return v
            return int(read_num() or 0)

        if C == "Z":
            if cur:
                cur["closed"] = True
                cx, cy = sx, sy
                cur = None          # a draw command after Z starts a fresh
            pcx = pqx = None        # subpath at the point Z closed back to
            continue
        if C == "M":
            x = read_num(); y = read_num()
            if x is None:
                break
            if rel and cur:
                x, y = cx + x, cy + y
            cx, cy = x, y
            sx, sy = x, y
            start_sub(x, y)
            cmd = "l" if rel else "L"   # subsequent pairs are implicit lineto
            pcx = pqx = None
            continue
        if cur is None:
            start_sub(cx, cy); sx, sy = cx, cy
        if C == "L":
            x = read_num(); y = read_num()
            if None in (x, y):
                break
            if rel:
                x, y = cx + x, cy + y
            cur["segs"].append(("L", x, y)); cx, cy = x, y
            pcx = pqx = None
        elif C == "H":
            x = read_num()
            if x is None:
                break
            x = cx + x if rel else x
            cur["segs"].append(("L", x, cy)); cx = x
            pcx = pqx = None
        elif C == "V":
            y = read_num()
            if y is None:
                break
            y = cy + y if rel else y
            cur["segs"].append(("L", cx, y)); cy = y
            pcx = pqx = None
        elif C == "C":
            x1 = read_num(); y1 = read_num(); x2 = read_num()
            y2 = read_num(); x = read_num(); y = read_num()
            if None in (x1, y1, x2, y2, x, y):
                break
            if rel:
                x1, y1, x2, y2, x, y = cx+x1, cy+y1, cx+x2, cy+y2, cx+x, cy+y
            cur["segs"].append(("C", x1, y1, x2, y2, x, y))
            pcx, pcy = x2, y2; cx, cy = x, y; pqx = None
        elif C == "S":
            x2 = read_num(); y2 = read_num(); x = read_num(); y = read_num()
            if None in (x2, y2, x, y):
                break
            if rel:
                x2, y2, x, y = cx+x2, cy+y2, cx+x, cy+y
            x1 = 2*cx - pcx if pcx is not None else cx
            y1 = 2*cy - pcy if pcx is not None else cy
            cur["segs"].append(("C", x1, y1, x2, y2, x, y))
            pcx, pcy = x2, y2; cx, cy = x, y; pqx = None
        elif C == "Q":
            qx = read_num(); qy = read_num(); x = read_num(); y = read_num()
            if None in (qx, qy, x, y):
                break
            if rel:
                qx, qy, x, y = cx+qx, cy+qy, cx+x, cy+y
            cur["segs"].append(_quad_to_cubic(cx, cy, qx, qy, x, y))
            pqx, pqy = qx, qy; cx, cy = x, y; pcx = None
        elif C == "T":
            x = read_num(); y = read_num()
            if None in (x, y):
                break
            if rel:
                x, y = cx+x, cy+y
            qx = 2*cx - pqx if pqx is not None else cx
            qy = 2*cy - pqy if pqx is not None else cy
            cur["segs"].append(_quad_to_cubic(cx, cy, qx, qy, x, y))
            pqx, pqy = qx, qy; cx, cy = x, y; pcx = None
        elif C == "A":
            rx = read_num(); ry = read_num(); rot = read_num()
            large = read_flag(); sweep = read_flag()
            x = read_num(); y = read_num()
            if None in (rx, ry, rot, x, y):
                break
            if rel:
                x, y = cx+x, cy+y
            cur["segs"].extend(_arc_to_cubics(cx, cy, rx, ry, rot, large, sweep, x, y))
            cx, cy = x, y; pcx = pqx = None
        else:
            break
    return subs


# --------------------------------------------------------------------------- #
# Basic shapes → subpaths (in the element's own user coordinates)
# --------------------------------------------------------------------------- #
def _f(el, name, default=0.0):
    v = el.get(name)
    if v is None:
        return default
    m = _NUM_RE.match(v.strip())
    return float(m.group()) if m else default


def _rect_subs(el):
    x, y = _f(el, "x"), _f(el, "y")
    w, h = _f(el, "width"), _f(el, "height")
    if w <= 0 or h <= 0:
        return []
    rx = el.get("rx"); ry = el.get("ry")
    rxv = _f(el, "rx") if rx is not None else (_f(el, "ry") if ry is not None else 0.0)
    ryv = _f(el, "ry") if ry is not None else rxv
    rxv, ryv = min(rxv, w / 2), min(ryv, h / 2)
    if rxv <= 0 or ryv <= 0:      # plain rectangle
        segs = [("L", x + w, y), ("L", x + w, y + h), ("L", x, y + h), ("L", x, y)]
        return [{"start": (x, y), "segs": segs, "closed": True}]
    # rounded rectangle: 4 edges + 4 corner arcs (sweep=1)
    seg = []
    seg.append(("L", x + w - rxv, y))
    seg += _arc_to_cubics(x + w - rxv, y, rxv, ryv, 0, 0, 1, x + w, y + ryv)
    seg.append(("L", x + w, y + h - ryv))
    seg += _arc_to_cubics(x + w, y + h - ryv, rxv, ryv, 0, 0, 1, x + w - rxv, y + h)
    seg.append(("L", x + rxv, y + h))
    seg += _arc_to_cubics(x + rxv, y + h, rxv, ryv, 0, 0, 1, x, y + h - ryv)
    seg.append(("L", x, y + ryv))
    seg += _arc_to_cubics(x, y + ryv, rxv, ryv, 0, 0, 1, x + rxv, y)
    return [{"start": (x + rxv, y), "segs": seg, "closed": True}]


def _ellipse_subs(cx, cy, rx, ry):
    if rx <= 0 or ry <= 0:
        return []
    start = (cx + rx, cy)
    seg = _arc_to_cubics(cx + rx, cy, rx, ry, 0, 1, 1, cx - rx, cy)
    seg += _arc_to_cubics(cx - rx, cy, rx, ry, 0, 1, 1, cx + rx, cy)
    return [{"start": start, "segs": seg, "closed": True}]


def _points_subs(el, closed):
    pts = [float(v) for v in _NUM_RE.findall(el.get("points", ""))]
    coords = list(zip(pts[0::2], pts[1::2]))
    if len(coords) < 2:
        return []
    segs = [("L", x, y) for x, y in coords[1:]]
    return [{"start": coords[0], "segs": segs, "closed": closed}]


def _shape_subs(el, tag):
    if tag == "path":
        return _parse_path_d(el.get("d", ""))
    if tag == "rect":
        return _rect_subs(el)
    if tag == "circle":
        return _ellipse_subs(_f(el, "cx"), _f(el, "cy"), _f(el, "r"), _f(el, "r"))
    if tag == "ellipse":
        return _ellipse_subs(_f(el, "cx"), _f(el, "cy"), _f(el, "rx"), _f(el, "ry"))
    if tag == "line":
        return [{"start": (_f(el, "x1"), _f(el, "y1")),
                 "segs": [("L", _f(el, "x2"), _f(el, "y2"))], "closed": False}]
    if tag == "polyline":
        return _points_subs(el, False)
    if tag == "polygon":
        return _points_subs(el, True)
    return []


def _bake(subs, M):
    """Apply affine M to every point of every subpath (in place, new list)."""
    out = []
    for sp in subs:
        segs = []
        for seg in sp["segs"]:
            if seg[0] == "L":
                segs.append(("L",) + _apply(M, seg[1], seg[2]))
            else:  # C
                x1, y1 = _apply(M, seg[1], seg[2])
                x2, y2 = _apply(M, seg[3], seg[4])
                x, y = _apply(M, seg[5], seg[6])
                segs.append(("C", x1, y1, x2, y2, x, y))
        out.append({"start": _apply(M, *sp["start"]), "segs": segs,
                    "closed": sp["closed"]})
    return out


# --------------------------------------------------------------------------- #
# Style resolution (inherited presentation attrs + inline style=)
# --------------------------------------------------------------------------- #
_INHERIT = ("stroke", "fill", "stroke-width", "stroke-opacity",
            "fill-opacity", "opacity", "fill-rule")


def _own_style(el) -> Dict[str, str]:
    st: Dict[str, str] = {}
    for k in _INHERIT:
        v = el.get(k)
        if v is not None:
            st[k] = v.strip()
    style = el.get("style")
    if style:
        for decl in style.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                k = k.strip()
                if k in _INHERIT:
                    st[k] = v.strip()
    return st


# --------------------------------------------------------------------------- #
# Length / viewport parsing
# --------------------------------------------------------------------------- #
def _len_to_mm(value: Optional[str], dpi: float, ref_mm: float = 0.0):
    """Parse an SVG length to mm. Unitless / px use `dpi`; % uses ref_mm."""
    if value is None:
        return None
    m = _LEN_RE.match(value)
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "px":
        return num * 25.4 / dpi
    if unit == "%":
        return num / 100.0 * ref_mm
    return num * _LEN_TO_MM[unit]


def _tag(el):
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


# --------------------------------------------------------------------------- #
# Document flatten
# --------------------------------------------------------------------------- #
_SKIP_DIRECT = {"defs", "symbol", "clipPath", "mask", "marker", "pattern",
                "linearGradient", "radialGradient", "metadata", "title",
                "desc", "style", "filter"}


def _is_hidden(el) -> bool:
    """True if the element (and its subtree) is display:none — e.g. an Inkscape
    hidden layer. We must NOT cut hidden geometry."""
    if el.get("display") == "none":
        return True
    style = el.get("style")
    return bool(style) and re.search(r"display\s*:\s*none", style) is not None


def _leaves_from(el, ctm, style, ids, warns, depth=0):
    """Recursively collect flattened leaves (mm-space subpaths + style)."""
    tag = _tag(el)
    if tag in _SKIP_DIRECT or _is_hidden(el):
        return []
    ctm = _mul(ctm, _parse_transform(el.get("transform")))
    style = {**style, **_own_style(el)}
    leaves: List[dict] = []

    if tag in ("g", "svg", "a", "switch"):
        for child in el:
            leaves += _leaves_from(child, ctm, style, ids, warns, depth)
        return leaves

    if tag == "use":
        if depth > 12:
            return leaves
        href = el.get("href") or el.get(f"{{{_XLINK_NS}}}href") or ""
        ref = ids.get(href.lstrip("#"))
        if ref is None:
            warns.setdefault("use", 0)
            warns["use"] += 1
            return leaves
        ux, uy = _f(el, "x"), _f(el, "y")
        use_ctm = _mul(ctm, (1, 0, 0, 1, ux, uy))
        # <use> of a <symbol>/<svg> renders that element's *children* (the
        # symbol itself is never drawn directly, so _leaves_from would skip it).
        if _tag(ref) in ("symbol", "svg"):
            rctm = _mul(use_ctm, _parse_transform(ref.get("transform")))
            rstyle = {**style, **_own_style(ref)}
            for child in ref:
                leaves += _leaves_from(child, rctm, rstyle, ids, warns, depth + 1)
            return leaves
        return _leaves_from(ref, use_ctm, style, ids, warns, depth + 1)

    if tag == "text":
        warns["text"] = warns.get("text", 0) + 1
        return leaves
    if tag == "image":
        warns["image"] = warns.get("image", 0) + 1
        return leaves

    subs = _shape_subs(el, tag)
    if subs:
        leaves.append({"subs": _bake(subs, ctm), "style": style,
                       "scale": _avg_scale(ctm), "id": el.get("id")})
    return leaves


def load_svg(source, p: EpiParams):
    """Parse an SVG file/string into flattened mm-space leaves + viewport info.

    Returns (leaves, width_mm, height_mm, warns). The root CTM maps the input's
    user-unit coordinate system straight to millimetres, so leaves come back
    already baked into real mm.
    """
    if isinstance(source, bytes):
        root = ET.fromstring(source)                     # bytes carry their own decl
    elif hasattr(source, "read"):
        root = ET.parse(source).getroot()
    elif isinstance(source, str) and source.lstrip().startswith("<"):
        # strip a leading <?xml ... encoding=...?> — ET rejects it on a str
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", source, count=1))
    else:
        root = ET.parse(source).getroot()

    # id table for <use> resolution (whole document, defs included).
    ids: Dict[str, ET.Element] = {}
    for el in root.iter():
        i = el.get("id")
        if i:
            ids[i] = el

    vb = root.get("viewBox")
    dpi = p.dpi
    w_attr_mm = _len_to_mm(root.get("width"), dpi)
    h_attr_mm = _len_to_mm(root.get("height"), dpi)
    if vb:
        vx, vy, vw, vh = [float(v) for v in re.split(r"[\s,]+", vb.strip())]
    else:
        # No viewBox: user units are the raw width/height numbers.
        vx, vy = 0.0, 0.0
        vw = _f(root, "width", 0.0) or (w_attr_mm or 0.0)
        vh = _f(root, "height", 0.0) or (h_attr_mm or 0.0)
    if vw <= 0 or vh <= 0:
        raise ValueError("SVG has no usable viewBox or width/height to size from")

    # Physical size, in mm. --width-mm forces it (fixing bad/junk metadata);
    # otherwise use the declared width, else fall back to treating the viewBox
    # as px at the given DPI.
    if p.width_mm is not None:
        width_mm = float(p.width_mm)
        height_mm = width_mm * (vh / vw)
    else:
        width_mm = w_attr_mm if w_attr_mm else vw * 25.4 / dpi
        height_mm = h_attr_mm if h_attr_mm else vh * 25.4 / dpi

    sx, sy = width_mm / vw, height_mm / vh
    root_ctm = (sx, 0.0, 0.0, sy, -vx * sx, -vy * sy)

    warns: Dict[str, int] = {}
    leaves: List[dict] = []
    root_style = _own_style(root)   # honour fill/stroke set on the <svg> itself
    for child in root:
        leaves += _leaves_from(child, root_ctm, root_style, ids, warns, 0)
    return leaves, width_mm, height_mm, warns


# --------------------------------------------------------------------------- #
# Colour audit + operation mapping
# --------------------------------------------------------------------------- #
# The Epilog driver runs one cut/score/engrave op per DISTINCT colour listed in
# its Color Mapping tab, matched by EXACT RGB. Two traps: (1) pure black is
# reserved — it always uses the General tab, never a map entry; (2) any colour
# that doesn't exactly match a map row is silently treated as black. So a file
# meant to have several ops needs a few clean, exact, non-black colours. --snap
# rounds every source colour to the nearest of these easy-to-enter primaries so
# each op is an exact RGB you can type into the map; the audit flags the traps.
_CANON = {
    "#ff0000": "red", "#00ff00": "green", "#0000ff": "blue",
    "#00ffff": "cyan", "#ff00ff": "magenta", "#ffff00": "yellow",
    "#ff8000": "orange", "#8000ff": "violet",
    "#000000": "black", "#ffffff": "white",
}
_NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000",
    "green": "#008000", "lime": "#00ff00", "blue": "#0000ff",
    "yellow": "#ffff00", "cyan": "#00ffff", "aqua": "#00ffff",
    "magenta": "#ff00ff", "fuchsia": "#ff00ff", "orange": "#ffa500",
    "gray": "#808080", "grey": "#808080", "none": "none", "transparent": "none",
}


def _norm_color(s: Optional[str]) -> Optional[str]:
    """Normalize a CSS colour to '#rrggbb' (or 'none'); None if unparseable."""
    if not s:
        return None
    s = s.strip().lower()
    if s in _NAMED:
        return _NAMED[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6 and all(c in "0123456789abcdef" for c in h):
            return "#" + h
        return None
    m = re.match(r"rgba?\(([^)]*)\)", s)
    if m:
        parts = re.split(r"[\s,]+", m.group(1).strip())[:3]
        try:
            rgb = [int(round(float(v[:-1]) * 2.55)) if v.endswith("%") else int(float(v))
                   for v in parts]
            return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in rgb)
        except ValueError:
            return None
    return None


def _source_color(style: Dict[str, str]) -> str:
    """The colour that identifies a path's intended operation: its stroke if it
    has one, else its fill, else black (SVG's default fill)."""
    stroke = _norm_color(style.get("stroke"))
    if stroke and stroke != "none":
        return stroke
    fill = _norm_color(style.get("fill", "black"))
    return fill if fill and fill != "none" else "#000000"


def _snap_color(hexc: str) -> str:
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return min(_CANON, key=lambda c: (int(c[1:3], 16) - r) ** 2
               + (int(c[3:5], 16) - g) ** 2 + (int(c[5:7], 16) - b) ** 2)


def analyze_colors(leaves, p: EpiParams):
    """Tally source colours per op, optionally snapping each leaf to a clean
    primary. Returns (rows, warnings): rows is [{src, out, count}], warnings is
    a list of human-readable Epilog colour-map gotchas."""
    rows: Dict[str, dict] = {}
    for leaf in leaves:
        src = _source_color(leaf["style"])
        out = _snap_color(src) if p.snap_colors else src
        if p.snap_colors:
            leaf["color"] = out
        r = rows.setdefault(src, {"src": src, "out": out, "count": 0})
        r["count"] += 1
    report = sorted(rows.values(), key=lambda r: -r["count"])
    outs = {r["out"] for r in report}

    warnings: List[str] = []
    if p.hairline and not p.snap_colors and len(rows) > 1:
        warnings.append(f"{len(rows)} distinct source colours were flattened to "
                        "a single cut stroke — use --snap (or --no-hairline) to "
                        "keep them as separate cut/score/engrave ops")
    if "#000000" in outs and len(outs) > 1:
        warnings.append("pure black is used alongside other colours — the "
                        "Epilog driver reserves black for the General tab, so "
                        "that op can't have its own Color Mapping row")
    if not p.snap_colors:
        muddy = sorted(o for o in outs if o not in _CANON and o != "none")
        if muddy:
            warnings.append("non-primary colours won't match a Color Mapping "
                            "row exactly and fall back to black: "
                            + ", ".join(muddy) + " — try --snap")
    return report, warnings


# --------------------------------------------------------------------------- #
# Emit  (relative 0.01 mm grid, same convention as linify)
# --------------------------------------------------------------------------- #
def _coords(nums):
    """Compactly join integer-grid numbers, dropping the space before a '-'."""
    s = _num(nums[0])
    for v in nums[1:]:
        sv = _num(v)
        s += ("" if sv.startswith("-") else " ") + sv
    return s


def _q(v):
    return int(round(v * _Q))


def _emit_d(subs) -> str:
    """Serialize baked mm subpaths to a relative path `d` (M/l/c/z)."""
    out = []
    cx = cy = 0
    for sp in subs:
        x, y = _q(sp["start"][0]), _q(sp["start"][1])
        out.append("M" + _coords([x, y]))
        cx, cy = x, y
        for seg in sp["segs"]:
            if seg[0] == "L":
                x, y = _q(seg[1]), _q(seg[2])
                out.append("l" + _coords([x - cx, y - cy]))
                cx, cy = x, y
            else:
                x1, y1 = _q(seg[1]), _q(seg[2])
                x2, y2 = _q(seg[3]), _q(seg[4])
                x, y = _q(seg[5]), _q(seg[6])
                out.append("c" + _coords([x1 - cx, y1 - cy, x2 - cx, y2 - cy,
                                          x - cx, y - cy]))
                cx, cy = x, y
        if sp["closed"]:
            out.append("z")
    return "".join(out)


def _leaf_attrs(leaf, p: EpiParams) -> str:
    """Path styling. --hairline collapses every path to a single fill:none
    hairline cut stroke (the laser invariant); otherwise faithful pass-through
    of the source stroke/fill/width, scaled to mm."""
    if p.hairline:
        return f'fill="none" stroke="{leaf.get("color", p.color)}" ' \
               f'stroke-width="{p.stroke_width}"'
    st = leaf["style"]
    fill = st.get("fill", "black")
    stroke = st.get("stroke", "none")
    parts = [f'fill="{fill}"', f'stroke="{stroke}"']
    if stroke != "none":
        sw = st.get("stroke-width", "1")
        m = _NUM_RE.match(sw.strip())
        sw_user = float(m.group()) if m else 1.0   # user units (unit suffix ignored)
        parts.append(f'stroke-width="{_fmt(sw_user * leaf["scale"])}"')
    if st.get("fill-rule"):
        parts.append(f'fill-rule="{st["fill-rule"]}"')
    return " ".join(parts)


def leaves_to_svg(leaves, width_mm, height_mm, p: EpiParams) -> str:
    """Wrap flattened leaves in a true-scale, transform-free SVG document."""
    per = _MM_PER_UNIT.get(p.units, 1.0)
    w, h = _fmt(width_mm), _fmt(height_mm)
    dw, dh = _fmt(width_mm / per), _fmt(height_mm / per)
    body = []
    for leaf in leaves:
        d = _emit_d(leaf["subs"])
        if not d:
            continue
        idattr = f' id="{leaf["id"]}"' if leaf.get("id") else ""
        body.append(f'  <path{idattr} d="{d}" {_leaf_attrs(leaf, p)}/>')
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1"\n'
        f'     width="{dw}{p.units}" height="{dh}{p.units}" viewBox="0 0 {w} {h}">\n'
        + "\n".join(body) + "\n"
        f"</svg>\n"
    )


# --------------------------------------------------------------------------- #
# Top-level API  (shared by CLI and any future web UI)
# --------------------------------------------------------------------------- #
def svg_to_epilog(source, p: EpiParams):
    """Rewrite `source` (path / file-like / SVG string) into an Epilog-safe SVG.

    Returns (svg_string, stats_dict).
    """
    if p.units not in _MM_PER_UNIT:
        raise ValueError(f"unknown units {p.units!r}; choose from {list(_MM_PER_UNIT)}")
    leaves, width_mm, height_mm, warns = load_svg(source, p)
    colors, color_warnings = analyze_colors(leaves, p)   # may set leaf["color"]
    svg = leaves_to_svg(leaves, width_mm, height_mm, p)
    per = _MM_PER_UNIT.get(p.units, 1.0)
    n_pts = sum(1 + len(sp["segs"]) for lf in leaves for sp in lf["subs"])
    stats = {
        "width_mm": round(width_mm, 3),
        "height_mm": round(height_mm, 3),
        "units": p.units,
        "width_disp": round(width_mm / per, 4),
        "height_disp": round(height_mm / per, 4),
        "paths": len(leaves),
        "points": n_pts,
        "warnings": warns,
        "colors": colors,
        "color_warnings": color_warnings,
    }
    return svg, stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    d = EpiParams()
    ap = argparse.ArgumentParser(
        description="Flatten transforms and fix true scale so an SVG imports "
                    "cleanly into the Epilog print driver.")
    ap.add_argument("input", help="input .svg (from Inkscape / Affinity / etc.)")
    ap.add_argument("-o", "--output",
                    help="output .svg (default: <input>_cut.svg next to the "
                         "input; pass '-' for stdout)")
    ap.add_argument("--width-mm", type=float, default=d.width_mm,
                    help="force physical width in mm (height keeps aspect); "
                         "use this when the file's declared size is wrong")
    ap.add_argument("--units", default=d.units, choices=list(_MM_PER_UNIT),
                    help="header unit (mm | cm | in)")
    ap.add_argument("--dpi", type=float, default=d.dpi,
                    help="px->mm assumption for unitless lengths "
                         "(96 modern Inkscape, 90 older, 72 Illustrator)")
    ap.add_argument("--hairline", action=argparse.BooleanOptionalAction,
                    default=d.hairline,
                    help="force every path to fill:none + a hairline cut stroke "
                         "(--no-hairline preserves the source styling)")
    ap.add_argument("--stroke-width", default=d.stroke_width,
                    help="hairline width in mm (default 0.02)")
    ap.add_argument("--color", default=d.color,
                    help="cut-stroke colour when hairline-normalizing")
    ap.add_argument("--snap", dest="snap_colors", action="store_true",
                    default=d.snap_colors,
                    help="snap each operation's colour to the nearest clean "
                         "primary so it matches an Epilog Color Mapping row exactly")
    ap.add_argument("--report", action="store_true",
                    help="print a colour / operation audit to stderr")
    return ap


def _params_from_args(ns) -> EpiParams:
    return EpiParams(width_mm=ns.width_mm, units=ns.units, dpi=ns.dpi,
                     hairline=ns.hairline, stroke_width=ns.stroke_width,
                     color=ns.color, snap_colors=ns.snap_colors)


def main(argv=None) -> int:
    ns = build_parser().parse_args(argv)
    p = _params_from_args(ns)
    try:
        svg, stats = svg_to_epilog(ns.input, p)
    except FileNotFoundError:
        print(f"error: input not found: {ns.input}", file=sys.stderr)
        return 1
    except (ET.ParseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if ns.report:
        _print_report(stats)
    for msg in stats["color_warnings"]:
        print(f"warning: {msg}", file=sys.stderr)

    out = ns.output or default_output_path(ns.input, "cut")
    if out == "-":
        sys.stdout.write(svg)
    else:
        with open(out, "w") as fh:
            fh.write(svg)
        w = warn_summary(stats["warnings"])
        print(f"wrote {out}  {stats['width_mm']}x{stats['height_mm']}mm  "
              f"{stats['paths']} paths / {stats['points']} pts{w}", file=sys.stderr)
    return 0


def _print_report(stats) -> None:
    rows = stats["colors"]
    print(f"— colour / operation audit ({len(rows)} distinct) —", file=sys.stderr)
    for r in rows:
        name = _CANON.get(r["out"], "")
        arrow = f" -> {r['out']} {name}".rstrip() if r["out"] != r["src"] else ""
        note = "  [reserved: General tab]" if r["out"] == "#000000" else ""
        print(f"  {r['count']:4d}x  {r['src']}{arrow}{note}", file=sys.stderr)


def warn_summary(warns: Dict[str, int]) -> str:
    if not warns:
        return ""
    label = {"text": "<text> (convert to paths)", "image": "raster <image>",
             "use": "unresolved <use>"}
    bits = [f"{v} {label.get(k, k)}" for k, v in warns.items()]
    return "  [skipped: " + ", ".join(bits) + "]"


if __name__ == "__main__":
    raise SystemExit(main())
