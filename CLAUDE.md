# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`laser-toolz` converts raster images into laser-ready hairline SVG line art for the Epilog laser cutter. One CLI (`linify.py`) with seven interchangeable render modes, plus an optional Flask web UI (`server.py`) that reuses the CLI's rendering core unchanged.

## The non-negotiable constraint

**The laser ignores stroke width, so tone is encoded by geometry only** — displacement, spacing, or contour density — never by line thickness or color. Every render mode must respect this. Output is always: fixed hairline `stroke-width` (default `0.02mm`), `fill:none`, a single stroke color, continuous `<path>` polylines (no dashes — perforation is added later in LightBurn). The SVG header carries real physical units with a matching `viewBox` so a `--width-mm 200` file imports at exactly 200mm.

## Commands

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate
pip install pillow numpy scikit-image   # scikit-image needed for contour/flow/tsp modes
pip install flask                        # only for the web UI

# CLI
python linify.py sample.png -o out.svg --mode wavy
python linify.py sample.png -o out.svg --mode contour --levels 8 --smooth 1.5

# web UI (defaults to port 5001 — macOS AirPlay squats on 5000)
python server.py
PORT=8080 python server.py

# eyeball geometry (hairlines are ~invisible on screen at true scale — bump width)
sed 's/stroke-width="0.02"/stroke-width="0.3"/' out.svg > preview.svg
```

There is no test suite, linter, or build step. `sample.png` is the synthetic test image used throughout the README.

## Architecture

The whole pipeline lives in **[linify.py](linify.py)** and flows in one direction:

1. **[Params](linify.py#L50)** — a single `@dataclass` holding every tunable. This is the source of truth for defaults, shared by the CLI arg parser and the web server. Global fields first, then per-mode field groups (wavy / spacing / contour / filet / flow / tsp). When adding a knob, add it here first.
2. **[load_gray](linify.py#L109)** — decodes to a grayscale float array at working resolution (`--resample`), applies `--invert`.
3. **A `render_*` function per mode**, all with the identical signature `(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]`. Each returns a list of polylines as `(N,2)` arrays in **millimeter coordinates**. Modes are dispatched through the **[`_MODES`](linify.py#L735) registry** dict — the single point that maps a mode name to its function.
4. **[polylines_to_svg](linify.py#L748)** — serializes polylines to the hairline SVG string (handles unit header via `_MM_PER_UNIT`, decimation formatting).
5. **[image_to_svg(source, p)](linify.py#L783)** — the public API tying it together; returns `(svg_string, stats_dict)`. Both the CLI (`main`) and the server call this, so **same Params → byte-identical SVG** regardless of entry point.

Shared geometry helpers used across modes: [`rdp`](linify.py#L188) (Ramer-Douglas-Peucker decimation), [`split_runs`](linify.py#L223) (break a polyline at masked/dropped points), [`sample_grid`](linify.py#L134) / [`sample_points`](linify.py#L161) (bilinear sampling of the gray field at mm coordinates).

**To add a render mode:** write a `render_yourmode(gray, p, width_mm, height_mm)` returning mm-space polylines, add its per-mode fields to `Params`, register it in `_MODES`, and add its args in `build_parser`. It then works in both the CLI and the web UI automatically.

**[server.py](server.py)** is a thin single-user Flask wrapper — no persistence. Uploaded images live in in-memory dicts keyed by a token (`_IMAGES` / `_NAMES`, capped at 24 entries). `/upload` decodes and stores, `/render` (POST JSON) returns SVG + stats for live preview, `/download` (GET) returns the same SVG as a named file attachment. Params are reconstructed from JSON via [`_params_from_json`](server.py#L56), which relies on explicit type-map sets (`_BOOL_FIELDS` / `_INT_FIELDS` / `_STR_FIELDS`, rest float) because `amp`/`mask_threshold` default to `None` and PEP 563 makes dataclass `.type` a string. When you add a `Params` field of a non-float type, add it to the matching set. The entire HTML/JS front-end is the inline `_PAGE` string at the bottom of the file.

## The seven modes (geometry, not thickness)

- **wavy** — displaced horizontal scanlines; darkness modulates wiggle amplitude (clamped `< spacing/2` so lines never cross). `--amp-gamma` and `--phase-jitter` are the pure-geometry contrast knobs.
- **spacing** — horizontal lines that pack denser in dark regions. `clean` style = clipped silhouette; `density` style = per-column accumulator for internal shading (dottier).
- **contour** — iso-brightness topographic lines via `skimage.measure.find_contours`.
- **filet** — filet-crochet grid of filled vs. open cells.
- **flow** — edge-tangent streamline hatching following a structure-tensor field.
- **tsp** — a single continuous line: weighted stipple points joined into one path by a Hilbert-curve seed, then refined with interleaved 2-opt + or-opt passes (`--tsp-improve`). The Hilbert seed avoids the long "jump-back" edges a nearest-neighbor tour leaves; or-opt relocates strays that 2-opt alone can't fix.
- **cyber** — cybersigilism: barbed tendrils that grow along the same structure-tensor tangent field as `flow` (so they wrap the form like veins/circuit-traces), curling harder toward a needle tip and sprouting thorns on alternating sides at intervals. Each thorn emerges *tangent* to the stem and curves off like a flame (`--cyber-barb-angle` = the curl arc) so the join reads smooth rather than as a straight stick across the line. Tone = seed density + tendril length (never thickness). The needle taper is a *shape*, not a stroke: `--cyber-spike sliver` renders each spike as a closed outline that converges to a point; `stroke` is a lighter single flick-out V. `--cyber-symmetry mirror` reflects the left half for a bilateral sigil composition; `--cyber-nodes` adds circuit-node circles at tendril roots. Reuses `flow`'s `_flow_tangent_field`.

See [README.md](README.md) for the full per-mode parameter reference and the parameter cheat-sheet table.
