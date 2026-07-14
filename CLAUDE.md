# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`laser-toolz` converts raster images into laser-ready hairline SVG line art for the Epilog laser cutter. One CLI (`linify.py`) with seven interchangeable render modes, plus an optional Flask web UI (`server.py`) that reuses the CLI's rendering core unchanged.

A **sibling tool**, `segment.py` (+ its interactive UI `segment_server.py`), does the deliberate opposite: it turns a photo into a **filled, multi-colour segmentation-mask SVG** for Inkscape via MobileSAM. It intentionally breaks the hairline/`fill:none` invariant below — that's why it's a separate file, not a linify mode — but reuses linify's mm-grid path encoders so output still imports at true scale. See its own section near the end.

A third sibling, `epilogue.py`, goes the *other* direction on the pipeline: it **ingests** an existing SVG (from any tool, including linify/segment) and rewrites it into the dialect the Epilog print driver reliably imports — flattening transforms, fixing true scale, and auditing operation colours. It's the last pass before the laser. See its own section near the end.

## The non-negotiable constraint

**The laser ignores stroke width, so tone is encoded by geometry only** — displacement, spacing, or contour density — never by line thickness or color. Every render mode must respect this. Output is always: fixed hairline `stroke-width` (default `0.02mm`), `fill:none`, a single stroke color, continuous `<path>` polylines (no dashes — perforation is added later in LightBurn). The SVG header carries real physical units with a matching `viewBox` so a `--width-mm 200` file imports at exactly 200mm.

## Commands

```bash
# setup
python3 -m venv .venv && source .venv/bin/activate
pip install pillow numpy scikit-image   # scikit-image needed for contour/flow/tsp/glyph modes
pip install freetype-py                  # only for the glyph mode (reads font outlines)
pip install flask                        # only for the web UI

# CLI
python linify.py sample.png -o out.svg --mode wavy
python linify.py sample.png -o out.svg --mode contour --levels 8 --smooth 1.5

# web UI (defaults to port 5001 — macOS AirPlay squats on 5000)
python server.py
PORT=8080 python server.py

# launch all three web UIs at once (linify :5001 · segment :5002 · epilogue :5003)
python toolz.py
python toolz.py segment epilogue   # subset; `linify=8080` overrides a port

# eyeball geometry (hairlines are ~invisible on screen at true scale — bump width)
sed 's/stroke-width="0.02"/stroke-width="0.3"/' out.svg > preview.svg
```

There is no test suite, linter, or build step. `sample.png` is the synthetic test image used throughout the README.

## Architecture

The whole pipeline lives in **[linify.py](linify.py)** and flows in one direction:

1. **[Params](linify.py#L50)** — a single `@dataclass` holding every tunable. This is the source of truth for defaults, shared by the CLI arg parser and the web server. Global fields first, then per-mode field groups (wavy / spacing / contour / filet / flow / tsp / glyph). When adding a knob, add it here first.
2. **[load_gray](linify.py#L109)** — decodes to a grayscale float array at working resolution (`--resample`), applies `--invert`.
3. **A `render_*` function per mode**, all with the identical signature `(gray, p: Params, width_mm, height_mm) -> List[np.ndarray]`. Each returns a list of polylines as `(N,2)` arrays in **millimeter coordinates**. Modes are dispatched through the **[`_MODES`](linify.py#L735) registry** dict — the single point that maps a mode name to its function.
4. **[polylines_to_svg](linify.py#L748)** — serializes polylines to the hairline SVG string (handles unit header via `_MM_PER_UNIT`, decimation formatting).
5. **[image_to_svg(source, p)](linify.py#L783)** — the public API tying it together; returns `(svg_string, stats_dict)`. Both the CLI (`main`) and the server call this, so **same Params → byte-identical SVG** regardless of entry point.

Shared geometry helpers used across modes: [`rdp`](linify.py#L188) (Ramer-Douglas-Peucker decimation), [`split_runs`](linify.py#L223) (break a polyline at masked/dropped points), [`sample_grid`](linify.py#L134) / [`sample_points`](linify.py#L161) (bilinear sampling of the gray field at mm coordinates).

**Output naming is one convention family-wide:** every CLI and web server names its result `<stem>_<tag>.svg`, where `stem` comes from linify's shared [`safe_stem`](linify.py) (strip dir+extension, collapse unsafe chars, fall back to the tool name) and `tag` is the tool's descriptor — linify `<mode>`, segment `mask`, epilogue `cut`. The CLIs derive the whole path with [`default_output_path`](linify.py): omit `-o` and it writes `<stem>_<tag>.svg` next to the input, `-o -` forces stdout, `-o <path>` is explicit. The three servers import `safe_stem` and send the same `<stem>_<tag>.svg` via `Content-Disposition`; the segment/epilogue front-ends read that header back (a tiny `dlName` helper) rather than hardcoding a constant. Because tags compose, a full chain reads as `portrait.png` → `portrait_wavy.svg` → `portrait_wavy_cut.svg`. When you add a tool, reuse these two helpers — don't reinvent a stem-derivation scheme.

**To add a render mode:** write a `render_yourmode(gray, p, width_mm, height_mm)` returning mm-space polylines, add its per-mode fields to `Params`, register it in `_MODES`, and add its args in `build_parser`. It then works in both the CLI and the web UI automatically.

**[server.py](server.py)** is a thin single-user Flask wrapper — no persistence. Uploaded images live in in-memory dicts keyed by a token (`_IMAGES` / `_NAMES`, capped at 24 entries). `/upload` decodes and stores, `/render` (POST JSON) returns SVG + stats for live preview, `/download` (GET) returns the same SVG as a named file attachment. Params are reconstructed from JSON via [`_params_from_json`](server.py#L56), which relies on explicit type-map sets (`_BOOL_FIELDS` / `_INT_FIELDS` / `_STR_FIELDS`, rest float) because `amp`/`mask_threshold` default to `None` and PEP 563 makes dataclass `.type` a string. When you add a `Params` field of a non-float type, add it to the matching set. The entire HTML/JS front-end is the inline `_PAGE` string at the bottom of the file.

## The seven modes (geometry, not thickness)

- **wavy** — displaced horizontal scanlines; darkness modulates wiggle amplitude (clamped `< spacing/2` so lines never cross). `--amp-gamma` and `--phase-jitter` are the pure-geometry contrast knobs.
- **spacing** — horizontal lines that pack denser in dark regions. `clean` style = clipped silhouette; `density` style = per-column accumulator for internal shading (dottier).
- **contour** — iso-brightness topographic lines via `skimage.measure.find_contours`.
- **filet** — filet-crochet grid of filled vs. open cells.
- **flow** — edge-tangent streamline hatching following a structure-tensor field.
- **tsp** — a single continuous line: weighted stipple points joined into one path by a Hilbert-curve seed, then refined with interleaved 2-opt + or-opt passes (`--tsp-improve`). The Hilbert seed avoids the long "jump-back" edges a nearest-neighbor tour leaves; or-opt relocates strays that 2-opt alone can't fix.
- **glyph** — ASCII / Unicode line art: a grid of glyphs whose ink density tracks tone. Needs `freetype-py`, which hands over each glyph's **native font outline** (drawn as hairlines — no tracing) plus a rasterizer reused to rank glyphs by ink coverage and tag each one's dominant stroke orientation. Cells pick by coverage (`--glyph-palette` sorted light→dark); with `--glyph-edge`, cells on a coherent edge instead pick the directional glyph whose stroke aligns with it (reuses the flow structure-tensor math). Palettes: `ascii` / `blocks` / `boxdraw` / `favorites` (bundled from a glyph-archive export), or import any [glyph-archive](https://mrinalghosh.github.io/glyph-archive/) JSON via `--glyph-json` (CLI) / the Import button (web). `_GLYPH_FONT_STACK` is the per-glyph font fallback; `--glyph-font` overrides. `--glyph-instance` is the one exception to the "modes return flat mm polylines → `polylines_to_svg`" contract: it returns a `GlyphInstances` (distinct glyph outlines + per-cell placements) that `image_to_svg` routes to `glyph_instances_to_svg`, emitting `<defs>` + one `<use transform="translate">` per cell for a much smaller file. Uses `transform` (not `x`/`y` attributes) because that's the only form older Affinity Designer's importer honors.

See [README.md](README.md) for the full per-mode parameter reference and the parameter cheat-sheet table.

## The segmentation sibling tool

**[segment.py](segment.py)** is a self-contained CLI, NOT a linify mode, because its output is filled multi-colour regions — the exact opposite of the laser hairline invariant. It mirrors linify's patterns: a single `SegParams` dataclass holds all tunables; `image_to_segmentation_svg(source, p)` is the public API returning `(svg, stats)`. Pipeline: `load_rgb` → MobileSAM `SamAutomaticMaskGenerator` (`generate_masks`, sorted large→small, area-filtered) → `_dedup_masks` (drop nested duplicates by IoU, since SAM emits a whole shape *and* its sub-parts) → `mask_to_rings` (cv2 `findContours` with `RETR_CCOMP`, holes rendered via `fill-rule="evenodd"`, decimated with linify's `rdp`) → `regions_to_svg` (one labelled `<path>` per region, Inkscape `inkscape:label`/layer attrs). It **imports linify's mm-grid encoders** (`_num`/`_pair`/`rdp`/`_MM_PER_UNIT`) so both tools share the true-scale coordinate convention, but has its own SVG document wrapper (linify's `_svg_document` hardcodes `fill:none`). Weights live at `weights/mobile_sam.pt` (gitignored, ~40 MB). Runs CPU-only: the automatic generator feeds float64 points that Apple MPS rejects, so MPS is not auto-picked.

**[segment_server.py](segment_server.py)** is a separate Flask app (port 5002) from `server.py`, wrapping MobileSAM's *prompted* `SamPredictor` for interactive click-to-pick. It holds one shared predictor with a per-image embedding cached across clicks (recomputed only on image switch, tracked by `_CUR_TOKEN`) and frozen masks in `_SESS[token]["accepted"]`. Endpoints: `/upload`, `/pick` (points → mask polygons for a live canvas overlay), `/add` `/undo` `/reset`, `/download` (builds the SVG by reusing `segment.py`'s `mask_to_rings`/`region_colors`/`regions_to_svg`). The prompted predictor casts prompts to float32, so unlike the CLI's automatic generator it **runs on Apple MPS**. Front-end is the inline `_PAGE` string at the bottom.

Dependencies for the segmentation tools (heavier than linify): `pip install torch torchvision opencv-python timm flask` + `pip install git+https://github.com/ChaoningZhang/MobileSAM.git`.

## The Epilog-preflight sibling tool

**[epilogue.py](epilogue.py)** is the pipeline's only *consumer* of SVG (linify/segment produce it; this ingests it) — a self-contained CLI, NOT a linify mode, because it takes an existing SVG in and emits an Epilog-driver-safe SVG out. Zero heavy deps: pure stdlib (`xml.etree`, `re`, `math`) plus linify's mm encoders (`_MM_PER_UNIT`/`_fmt`/`_num`/`_Q`). It mirrors the family patterns: a single `EpiParams` dataclass holds all tunables; `svg_to_epilog(source, p)` is the public API returning `(svg, stats)`; output rides linify's compact 0.01 mm relative grid with a true-scale header.

The core is a **single matrix pass**. `load_svg` parses the tree, builds an id table for `<use>` resolution, and seeds a **root CTM that maps the input's user units straight to millimetres** (from the declared `width`/`height`+`viewBox`, or a forced `--width-mm`, or `viewBox`-as-px at `--dpi`). `_leaves_from` recurses, composing each element's `transform` into the CTM and merging inherited presentation/`style=` attrs, then `_bake` applies the CTM to every point — so **flattening transforms and fixing scale are the same operation**. All geometry is normalized to absolute line/cubic subpaths first: `_parse_path_d` (full path grammar, arcs→cubics via `_arc_to_cubics`, quadratics elevated, S/T reflection, arc-flag parsing), basic shapes via `_shape_subs`, `<use>` resolved against the id table (depth-guarded). `_emit_d` serializes back on the mm grid.

Three layered concerns, each its own knob: (1) **hairline normalization** (`_leaf_attrs`, default on) collapses every path to the laser invariant — `fill:none` + one hairline cut stroke — or `--no-hairline` passes source styling through (stroke-width scaled by the CTM's `_avg_scale`); (2) **colour audit / op mapping** (`analyze_colors`) tallies distinct source colours, and `--snap` rounds each to the nearest `_CANON` primary so it matches an Epilog **Color Mapping** row exactly — dodging the driver's two traps (pure black is reserved for the General tab; unmatched colours silently become black), surfaced via `--report` and stderr warnings. `<text>`/`<image>`/CSS-`<style>`-class styling are unsupported → counted in `warns` and skipped (inline `style=` and presentation attrs are read); `display:none` subtrees are dropped (never cut a hidden layer). Note the deliberate asymmetry with linify's `--glyph-instance`, which *needs* `transform` because that's the only form old Affinity honors: epilogue exists precisely because the **Epilog driver is the opposite** — it drops geometry under a transform, so everything gets baked flat.

**[toolz.py](toolz.py)** is the umbrella launcher: it runs each of the three servers as its own subprocess (they each call `app.run()`, so they can't share a process), handing each its port via the `PORT` env var it already honours, prefixing its logs, and tearing the whole set down on Ctrl-C **or** SIGTERM (routed through the same path so a `kill` doesn't orphan children). A dead child (missing dep, port in use) is reported but the others keep serving. It also exports `TOOLZ_<NAME>_URL` (defaults + any `name=port` override) to every child so the switch-nav links resolve to the real ports. Pure stdlib. **[toolz_nav.py](toolz_nav.py)** is the shared header nav all three pages inject: each server's index route swaps a `<!--TOOLZ-NAV-->` marker in its `_PAGE` header for `nav_html("<its own name>")`; `nav_html` reads `TOOLZ_<NAME>_URL` (else the default port) and styles itself purely from the CSS vars the three pages already share (`--fg`/`--mut`/`--line`/`--acc`), so it fits every theme with no extra CSS. If you restructure a page's `<header>`, keep the marker.

**[epilogue_server.py](epilogue_server.py)** is a third Flask app (port 5003), sharing the `server.py`/`segment_server.py` theme. Since epilogue has no model and is instant, the whole UI is live: `/upload` stores the raw SVG bytes, `/render` (POST JSON) re-runs `svg_to_epilog` on every toggle change for a before/after preview + a colour-audit panel, `/download` returns the same SVG. `_params_from_json` mirrors the CLI (width_mm float-or-None, dpi float, hairline/snap bools, rest strings). Every optimization option is a switch/field with an info tooltip; tooltips render into one shared fixed `#tipbox` so they escape the scrolling panel's clip. Front-end is the inline `_PAGE` string at the bottom.
