# laser-toolz

Tools to generate and manipulate SVGs for the Epilog laser cutter.

## linify — image → laser-ready SVG line art

Convert any raster image into **continuous hairline SVG lines** for laser
cutting / perforation. Six interchangeable render modes, one parametric CLI,
plus an optional local web UI for live experimentation.

Everything is built around one non-negotiable rule: **the laser ignores stroke
width, so tone is encoded by _geometry only_** — displacement, spacing, or
contours — never by line thickness or color.

## Why the output looks the way it does (laser constraints)

- **Every line is a hairline.** Fixed `stroke-width` (default `0.02mm`),
  `fill:none`, a single stroke color (default `black`; use `--color red` for a
  cut layer). Width is *not* a tonal variable.
- **Continuous paths, no dashes.** You add perforation in LightBurn / your
  laser software. Output is plain `<path>` polylines.
- **Real-world units.** SVG `width`/`height` carry a real physical size with a
  matching `viewBox`, so a `--width-mm 200` request imports as exactly 200 mm
  wide in LightBurn / Illustrator. Height is derived from the image aspect ratio.
  Pass `--units in` (or `cm`) to relabel the header for inch-only software — the
  geometry is untouched, so an 8-inch file still imports at true scale.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pillow numpy scikit-image   # scikit-image (+ its scipy) needed for contour, flow, tsp, glyph
pip install freetype-py                  # only for glyph mode (reads glyph outlines from fonts)
pip install flask                        # only for the optional web UI
python fetch_fonts.py                    # only for glyph mode — installs the bundled font packs
```

## Quick start

```bash
python linify.py sample.png -o out.svg --mode wavy      # zero tuning needed
```

## The seven modes

### 1. `wavy` — displaced scanlines (the "face-in-lines" look)

Horizontal carrier waves; **darkness modulates the wiggle amplitude** (dark =
big wiggle). Amplitude is clamped to `< line-spacing/2` so adjacent lines can
never cross.

```bash
python linify.py sample.png -o wavy.svg --mode wavy \
    --line-spacing 2 --wavelength 8 --amp 1

# add subject isolation (blank background) + frequency modulation in shadows
python linify.py sample.png -o wavy.svg --mode wavy \
    --mask-threshold 0.9 --freq-mod
```

**Getting contrast out of it.** Amplitude alone is a weak tonal cue (line count
and spacing are identical in light and dark; only the wiggle size changes), and a
shared phase makes every bulge line up into vertical banding. Two pure-geometry
knobs fix that:

- **`--amp-gamma`** (`<1`, try `0.5`) lifts midtones up the amplitude curve, so
  mid-gray actually wiggles hard instead of rippling gently. The default linear
  map only nears full swing at pure black, which is why untuned output looks flat.
- **`--phase-jitter`** (`0..1`, try `0.5`) gives each scanline its own phase so
  bulges stop aligning into columns — the surface reads as woven, organic ink.

```bash
python linify.py sample.png -o wavy.svg --mode wavy \
    --amp-gamma 0.5 --phase-jitter 0.6
```

### 2. `spacing` — density lines

Horizontal lines that **pack closer in dark regions** and spread apart in light
ones. Implemented by walking top→bottom, accumulating a darkness integral, and
emitting a line each time it crosses a threshold. `--spacing-style` chooses how
the horizontal axis is used — the difference between an outline and a full
tonal rendering:

- **`clean`** (default): vertical packing is driven by each row's mean darkness,
  giving crisp continuous horizontal lines, then each line is **clipped to the
  columns locally dark enough to want it** — so lines break across light gaps and
  terminate at the subject. You get the **silhouette / form**, clean enough to
  cut, with no internal noise.
- **`density`**: every column carries its own accumulator, so ink lands
  per-(row, column) — dark columns fire often and fuse into horizontal runs,
  light columns stay bare. This recovers **internal shading** (a real tonal
  portrait) at the cost of a **dithery / broken** look near tonal boundaries.

```bash
python linify.py sample.png -o spacing.svg --mode spacing \
    --min-spacing 0.6 --max-spacing 4 --spacing-style clean

python linify.py sample.png -o spacing.svg --mode spacing \
    --spacing-style density        # detailed shading, dottier
```

### 3. `contour` — topographic iso-lines

Quantizes brightness into `--levels` bands and traces each iso-brightness
contour with `skimage.measure.find_contours`. Organic, follows the form (the
displacement-map look).

```bash
python linify.py sample.png -o contour.svg --mode contour \
    --levels 8 --smooth 1.5 --min-contour-len 3
```

### 4. `filet` — crochet grid (filled vs. open cells)

Quantizes the image into a grid of **filled and open cells**, exactly like a
[filet crochet](https://en.wikipedia.org/wiki/Filet_crochet) chart. The image is
divided into `--cells-wide` columns (rows derived to keep cells square); a cell
becomes **filled** when its mean darkness clears `--fill-threshold`, and open
cells stay empty mesh windows. Tone is binary and **purely geometric** — a
filled cell carries a *mark*, never a heavier stroke.

The mesh lattice (every cell border) is drawn as long continuous horizontal and
vertical hairlines — real filet mesh, and efficient laser travel. `--fill-style`
picks how a filled cell reads:

- **`x`** (default): two diagonals — the classic filet chart cross.
- **`cross`**: a `+` (mid vertical + horizontal).
- **`hatch`**: `--hatch-lines` parallel diagonals packed into the cell — the most
  solid / tonal look.

`--no-mesh` (or enabling `--mask-threshold`) drops the background lattice so only
filled cells are drawn, each with its own outline, floating on blank ground.

```bash
python linify.py sample.png -o filet.svg --mode filet \
    --cells-wide 60 --fill-threshold 0.5 --fill-style x

# solid tonal blocks, no background grid, isolated subject
python linify.py sample.png -o filet.svg --mode filet \
    --cells-wide 48 --fill-style hatch --hatch-lines 4 --no-mesh --mask-threshold 0.85
```

### 5. `flow` — edge-tangent hatching

Short **streamlines that flow *along* the form** — around the eyes, down the
jaw, following folds — the way an illustrator lays down contour hatching. The
direction field comes from the *smoothed structure tensor* (not the raw
gradient), so it stays coherent even across noisy flat regions. **Tone is stroke
density**: each seed on a jittered grid survives with probability
`darkness**flow-gamma`, so strokes crowd into shadows and thin out in
highlights. Never stroke width — pure geometry.

```bash
python linify.py sample.png -o flow.svg --mode flow \
    --flow-spacing 1.4 --flow-len 7 --flow-smooth 6

# denser, longer, more coherent strokes; lift midtones for fuller coverage
python linify.py sample.png -o flow.svg --mode flow \
    --flow-spacing 1.0 --flow-len 12 --flow-smooth 10 --flow-gamma 0.6
```

- **`--flow-smooth`** is the coherence knob: higher averages the flow direction
  over a wider area (smoother, calmer lines), lower follows fine detail and noise.
- **`--flow-spacing`** sets seed pitch (hatching density); **`--flow-len`** sets
  how long each stroke runs before it stops — short reads as a woven stipple,
  long as flowing ink.

### 6. `tsp` — single continuous line

The **whole image in one unbroken stroke**. The picture is first *stippled* into
dots whose density tracks darkness (`--points` of them, weighted by
`--point-gamma`), then every dot is joined into a single **traveling-salesman
path**: seeded along a **Hilbert space-filling curve** (which never makes a long
jump-back edge), then cleaned up with interleaved neighbor-limited **2-opt +
or-opt** (`--tsp-improve` passes) — 2-opt uncrosses, or-opt relocates the strays
2-opt can't reach. The output is exactly **one `<path>`** — ideal for a single
continuous cut/engrave with no pen-up.

```bash
python linify.py sample.png -o tsp.svg --mode tsp \
    --points 4000 --tsp-improve 2

# denser line, isolated subject, midtones lifted for fuller shading
python linify.py sample.png -o tsp.svg --mode tsp \
    --points 8000 --point-gamma 0.7 --tsp-improve 3 --mask-threshold 0.9
```

- **`--points`** trades detail for compute: more dots = a longer, denser line
  and a slower tour.
- **`--tsp-improve`** `0` is the raw Hilbert seed (fast, some gap-crossing
  edges); `2–4` refines it into a cleaner line — each extra pass shortens the
  longest jumps at some CPU cost, plateauing once the tour is locally optimal.

Stippling is darkness-weighted rejection sampling (dot density is tone-accurate
but not blue-noise), and the tour is a heuristic, not an optimal TSP solution —
both are chosen so a few-thousand-dot portrait renders in well under a second.

### 7. `glyph` — ASCII / Unicode line art (needs `freetype-py`)

A grid of **characters** whose ink density tracks tone — an ASCII-art portrait,
except every glyph is drawn as the font's **own vector outline** (crisp
hairlines, no tracing), so it's laser-native. Each glyph in the palette is
analyzed once: `freetype-py` hands over its native outline *and* rasterizes it so
the mode can **rank glyphs by ink coverage** (light → dark) and **tag each one's
dominant stroke orientation**. Every image cell then picks the glyph whose
coverage matches its darkness. Tone is *which glyph* (how much contour packs into
the cell), never stroke thickness — same laser rule as every other mode.

**Edge-direction aware.** With `--glyph-edge > 0`, any cell sitting on a coherent
edge instead picks the **directional glyph whose stroke best aligns with that
edge** (reusing the same structure-tensor field as `flow`), blended against the
tonal match by the edge strength. It shines with the `boxdraw` palette, whose
`─ │ ╱ ╲ ┼` glyphs get tagged by angle and snap to the form's contours.

```bash
# density ramp — classic ASCII look
python linify.py sample.png -o glyph.svg --mode glyph --glyph-palette ascii --glyph-cols 100

# edge-aware line drawing with box-drawing glyphs
python linify.py sample.png -o glyph.svg --mode glyph \
    --glyph-palette boxdraw --glyph-edge 0.8 --glyph-cols 90

# your own character set from a glyph-archive export, in the unifont pack
python linify.py sample.png -o glyph.svg --mode glyph \
    --glyph-json ~/Downloads/utf-8-collection.json --glyph-pack unifont --glyph-cols 80
```

> **First run:** `python fetch_fonts.py` downloads the bundled font packs into
> `fonts/` (the three small OFL faces are committed; this pulls Unifont, ~5 MB).

- **`--glyph-palette`** is a built-in set — `ascii` (` .:-=+*#%@`), `blocks`
  (shades `░▒▓█`), `boxdraw` (directional lines), or `favorites` (a set bundled
  from a [glyph-archive](https://mrinalghosh.github.io/glyph-archive/) export).
  Any palette is re-sorted by *measured* coverage, so ordering is font-honest.
- **`--glyph-chars " .:-=+*#%@"`** or **`--glyph-json FILE`** override the palette
  with an explicit set (JSON accepts a glyph-archive export: `custom` + `favorites`
  codepoints). Glyphs no font in the pack can draw are dropped — see `--glyph-pack`.
- **`--glyph-pack`** picks which fonts draw the glyphs: `system` (macOS native,
  default) · `sans` (Helvetica) · `mono` (JetBrains Mono) · `serif` (Spectral) ·
  `unifont` (GNU Unifont — a blocky-pixel look with **near-total Unicode coverage**).
  The pack's primary font styles the common glyphs; a shared fallback tail (Noto
  Sans Symbols 2 + Apple Symbols / STIX / Hiragino) catches exotic symbol, arrow,
  alchemical and CJK-description codepoints — so an imported symbol set renders in
  full instead of dropping. The CLI **warns** if any glyph has no outline in the
  chosen pack (the web UI shows a live *"✓ all N"* / *"⚠ N unsupported"* badge);
  reach for `unifont` when a pack can't cover an exotic import.
- **`--glyph-edge`** `0` is pure density; `0.5–1` biases edge cells toward
  aligned strokes. **`--glyph-edge-threshold`** gates how strong an edge must be
  before a directional swap is allowed.
- **`--glyph-cols`** sets detail; **`--glyph-gamma`** lifts midtones;
  **`--glyph-size`** / **`--glyph-aspect`** tune glyph fill and cell shape;
  **`--glyph-font`** forces a single `.ttf`/`.otf` (overrides `--glyph-pack`).

Shade glyphs (the `blocks` palette) rasterize to *many* tiny contours, so they're
path-heavy — great tone, larger files; `ascii` / `boxdraw` / `favorites` stay
lean.

**`--glyph-instance`** shrinks path-heavy grids further: instead of repeating a
glyph's outline in every cell, it defines each distinct glyph once in `<defs>`
and drops one `<use transform="translate(…)">` per cell. On a dense grid that's
commonly a 3–5× smaller file (the bundled sample: 1.45 MB → ~280 KB), and the
resolved geometry is identical to within the 0.01 mm coordinate grid. The catch
is it relies on SVG `<use>`/`<defs>`: Inkscape and Illustrator flatten them on
import, but some importers don't — notably older **Affinity Designer** ignored
the `x`/`y` attributes on `<use>` (this uses `transform="translate"`, the form
Affinity's own fix adopted, but **verify a proof in your importer before a real
cut**). Off by default; it lives under **Advanced** in the web UI.

## Background masking

`--mask-threshold T` skips drawing anywhere the (effective) brightness is above
`T`, so a near-white background (e.g. a horse on white) produces **no lines** —
only the subject is drawn. `--invert` flips the tonal encoding *and* what counts
as background, so masking still "respects invert".

```bash
python linify.py horse.png -o horse.svg --mode wavy --mask-threshold 0.92
```

## Parameter cheat-sheet

| Flag | Default | Applies to | What it does |
|------|---------|-----------|--------------|
| `--mode` | `wavy` | all | `wavy` \| `spacing` \| `contour` \| `filet` \| `flow` \| `tsp` \| `glyph` |
| `-o, --output` | stdout | all | output SVG path |
| `--width-mm` | `200` | all | physical output width in mm (height from aspect) |
| `--units` | `mm` | all | header unit: `mm` \| `cm` \| `in` (`--width-mm` stays mm) |
| `--stroke-width` | `0.02` | all | hairline width in mm (tonally irrelevant) |
| `--color` | `black` | all | single stroke color (`red` = cut layer) |
| `--invert` | off | all | flip dark↔light tonal encoding |
| `--mask-threshold` | none | all | skip where brightness > T (erase background) |
| `--samples` | `800` | wavy, spacing | point density sampled along each line |
| `--decimate` | `0.03` | all but filet | collinear-point removal tolerance (mm) — smaller = more points |
| `--resample` | `900` | all | working image resolution, longest edge (px) |
| `--line-spacing` | `2` | wavy | mm between scanline baselines (sets line count) |
| `--amp` | spacing/2 | wavy | max wiggle amplitude mm (auto-clamped < spacing/2) |
| `--amp-gamma` | `1.0` | wavy | amplitude response curve; `<1` lifts midtones (contrast) |
| `--phase-jitter` | `0` | wavy | per-line phase decorrelation `0..1` (breaks banding) |
| `--wavelength` | `8` | wavy | carrier wavelength in mm |
| `--freq-mod` | off | wavy | also raise spatial frequency in dark regions |
| `--freq-amount` | `1.0` | wavy | strength of `--freq-mod` (0..~2) |
| `--min-spacing` | `0.6` | spacing | mm between lines in the darkest regions |
| `--max-spacing` | `4` | spacing | mm between lines in the lightest regions |
| `--spacing-style` | `clean` | spacing | `clean` (silhouette) \| `density` (internal shading) |
| `--levels` | `8` | contour | number of brightness bands |
| `--smooth` | `0` | contour | Gaussian blur sigma pre-contour (px), try 1–2 |
| `--min-contour-len` | `2` | contour | drop contours shorter than this (mm) |
| `--cells-wide` | `60` | filet | grid columns (rows derived to keep cells square) |
| `--fill-threshold` | `0.5` | filet | cell fills when darkness ≥ T (0..1) |
| `--fill-style` | `x` | filet | filled-cell mark: `x` \| `cross` \| `hatch` |
| `--hatch-lines` | `3` | filet | parallel diagonals per cell for `--fill-style hatch` |
| `--mesh` / `--no-mesh` | on | filet | draw the full grid lattice (off = filled cells only) |
| `--flow-smooth` | `6.0` | flow | structure-tensor blur sigma (px) — field coherence |
| `--flow-spacing` | `1.4` | flow | mm between seed points (hatching density) |
| `--flow-len` | `7.0` | flow | mm arc length of each streamline stroke |
| `--flow-step` | `0.4` | flow | mm integration step along a streamline |
| `--flow-gamma` | `1.0` | flow | seed-density response curve; `<1` lifts midtones |
| `--points` | `4000` | tsp | target stipple dot count (tour vertices) |
| `--point-gamma` | `1.0` | tsp | darkness weighting for dot density; `<1` lifts midtones |
| `--tsp-improve` | `2` | tsp | interleaved 2-opt + or-opt passes (`0` = raw Hilbert seed) |
| `--glyph-cols` | `80` | glyph | grid columns (rows from `--glyph-aspect`) |
| `--glyph-palette` | `ascii` | glyph | `ascii` \| `blocks` \| `boxdraw` \| `favorites` |
| `--glyph-chars` | — | glyph | explicit character set (overrides `--glyph-palette`) |
| `--glyph-json` | — | glyph | load a glyph-archive export as the character set |
| `--glyph-pack` | `system` | glyph | font pack: `system` \| `sans` \| `mono` \| `serif` \| `unifont` |
| `--glyph-font` | — | glyph | single font file (`.ttf`/`.otf`); overrides `--glyph-pack` |
| `--glyph-gamma` | `1.0` | glyph | darkness response curve; `<1` lifts midtones |
| `--glyph-size` | `0.92` | glyph | glyph size as a fraction of the cell |
| `--glyph-aspect` | `1.0` | glyph | cell height/width ratio (`1` = square) |
| `--glyph-edge` | `0` | glyph | edge-direction awareness `0..1` (`0` = pure density) |
| `--glyph-edge-threshold` | `0.12` | glyph | min edge coherence to allow a directional swap |
| `--glyph-instance` | off | glyph | define each glyph once in `<defs>`, place with `<use>` (smaller file; verify importer) |

## Verifying the output

```bash
python linify.py sample.png -o out.svg --mode wavy
head -4 out.svg          # -> width="200mm" height="250mm" viewBox="0 0 200 250"
```

Hairline strokes are (by design) nearly invisible in a browser at screen scale;
that's correct for a laser. To eyeball the geometry, temporarily bump the width:

```bash
sed 's/stroke-width="0.02"/stroke-width="0.3"/' out.svg > preview.svg
rsvg-convert -w 600 preview.svg -o preview.png   # or just open preview.svg
```

## Optional: local web UI

A tiny Flask server for dialing in parameters visually — upload an image, pick a
mode, drag sliders (or type exact values into the paired number boxes), see the
SVG update live, then download it. Extras beyond the CLI ergonomics:

- **Units selector** (mm / in / cm) — set the width in your unit and the file
  exports with a matching header, so it imports at true scale in inch-only tools.
- **Source-named downloads** — the file saves as `<input-name>_<mode>.svg`
  (e.g. `portrait_wavy.svg`), not a generic name. Every tool in the suite shares
  the same `<input>_<tag>.svg` scheme — `_<mode>` here, `_mask` for segment,
  `_cut` for epilogue — so a chain reads back as `portrait_wavy_cut.svg`.
- **Advanced panel** — exposes the finer knobs (`samples`, `resample`,
  `decimate`, `stroke-width`, `freq-amount`) for granular tuning.
- **Glyph palette import** — in `glyph` mode, an **Import JSON…** button loads a
  [glyph-archive](https://mrinalghosh.github.io/glyph-archive/) export as a custom
  character set (parsed in the browser), with a link straight to the archive.

```bash
pip install flask
python server.py                 # -> http://127.0.0.1:5001
PORT=8080 python server.py       # override the port if you like
```

> On macOS, port 5000 is taken by the AirPlay Receiver (it returns 403 and looks
> like a crash), so the server defaults to **5001**.

## segment — image → editable segmentation-mask SVG

A **sibling tool** to linify that does the opposite thing on purpose: instead of
hairline line art, it produces **filled, multi-colour region shapes** you can
drop straight into Inkscape and edit by hand. It uses
[MobileSAM](https://github.com/ChaoningZhang/MobileSAM) (a ~40 MB distilled
Segment Anything model) to find objects, traces each mask to a vector contour,
and writes one labelled `<path>` per region.

> This deliberately breaks linify's laser invariant (`fill:none`, one colour,
> tone-by-geometry) — which is exactly why it lives in its own file rather than
> as a linify `--mode`. It reuses linify's mm-grid path encoders, so output still
> imports at true physical scale.

**Install** (heavier than linify — pulls in PyTorch):

```bash
pip install torch torchvision opencv-python timm flask
pip install git+https://github.com/ChaoningZhang/MobileSAM.git
```

The ~40 MB MobileSAM checkpoint (`weights/mobile_sam.pt`, gitignored) is
**downloaded automatically on first run** — both the CLI and the web UI call
`segment.ensure_checkpoint`, which streams it into place and is a no-op
thereafter. To pre-fetch it (or if you're offline behind a proxy), grab it
manually:

```bash
curl -L -o weights/mobile_sam.pt \
  https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt
```

**CLI — automatic "segment everything":**

```bash
python segment.py photo.jpg                                    # -> photo_mask.svg
python segment.py photo.jpg -o mask.svg --color mean --max-regions 40 --layers
```

`-o` is optional — omit it and the file auto-names `<input>_mask.svg` next to the
source (pass `-o -` for stdout).

Each region becomes a separately selectable object in Inkscape's Objects panel
(`id` + `inkscape:label`, plus `data-area-mm2` / `data-iou`), under a single
`segmentation` layer — or one layer per region with `--layers`.

Key knobs:

- **`--color`** — `label` (distinct palette, default), `mean` (average image
  colour per region), or `gray` (grayscale ramp).
- **`--max-regions N`** — keep only the N largest regions.
- **`--dedup-iou`** (default `0.7`) — SAM emits nested masks (a whole shape *and*
  a sub-part); this drops any mask that overlaps a larger kept one above the
  threshold. Set `0` to keep everything.
- **`--min-area`** — drop regions below this fraction of the image.
- **`--simplify`** — contour decimation tolerance in mm.
- **`--points-per-side`** — SAM's sampling grid density (more = more/smaller
  regions, slower).

> Runs on **CPU** — MobileSAM's automatic generator feeds float64 point tensors
> that Apple's MPS backend rejects, so MPS isn't auto-picked. Expect tens of
> seconds for a typical image.

**Interactive — click-to-pick** (`segment_server.py`):

```bash
python segment_server.py           # -> http://127.0.0.1:5002
PORT=8080 python segment_server.py
```

Upload an image, then **left-click objects to include** them and **shift-click to
exclude** (carve the mask back); **Add region** freezes each mask, and **Download
SVG** writes them all out. Same output format as the CLI. Unlike the automatic
generator, the prompted predictor casts prompts to float32, so this UI runs on
**Apple MPS** when available.

## epilogue — make any SVG Epilog-safe

A second **sibling tool**. Where linify and segment *generate* SVG, epilogue
*ingests* one — from Inkscape, Affinity Designer, Illustrator, or linify/segment
themselves — and rewrites it into the one dialect the Epilog print driver
reliably imports. It's the pass that runs *after* your art tool, fixing the two
most common driver failures, both purely geometric:

- **Wrong scale.** Inkscape assumes 96 (older: 90) DPI while the driver reads
  72, so files land 25–33% too big; Affinity 2 exports at an arbitrary scale.
  epilogue reads the declared physical size — or a `--width-mm` you force — and
  re-emits in real millimetres with a matching `viewBox`, so it imports at
  exactly the intended size regardless of the source tool.
- **Unrendered transforms.** The driver silently drops any `<path>` inside a
  `transform` (a group translate/scale, a matrix, a `<use>`). epilogue composes
  every transform down the tree and *bakes* it into the coordinates, emitting
  flat, transform-free paths.

The flatten and rescale are one matrix pass (the root CTM maps user units
straight to mm). Basic shapes, arcs, and quadratics are reduced to line/cubic
paths; `<use>` is resolved; output uses linify's compact 0.01 mm grid.

```bash
python epilogue.py drawing.svg                            # flatten -> drawing_cut.svg
python epilogue.py drawing.svg -o cut.svg --width-mm 200  # force physical width
python epilogue.py old.svg     -o cut.svg --dpi 90        # old-Inkscape px files
```

Like the other tools, `-o` is optional — omitting it writes `<input>_cut.svg`
beside the source (pass `-o -` for stdout).

By default every path is normalized to the laser invariant — `fill:none` + a
single `0.02mm` hairline cut stroke — so nothing is mistaken for a raster
engrave. `--no-hairline` preserves the source stroke/fill/width (scaled to mm)
for files that are already styled correctly.

For multi-operation files (cut/score/engrave on different colours), `--report`
prints a colour audit and `--snap` rounds each op's colour to the nearest clean
primary so it matches an Epilog **Color Mapping** row *exactly*. This dodges the
driver's two traps: pure black is reserved for the General tab, and any colour
that doesn't exactly match a map row is silently treated as black.

```bash
python epilogue.py multi.svg -o cut.svg --report          # audit the op colours
python epilogue.py multi.svg -o cut.svg --snap            # clean them to exact primaries
```

`<text>` (convert to paths first), raster `<image>`, and CSS-class styling in a
`<style>` block are not handled — they're warned about and skipped (inline
`style=` and presentation attributes *are* read).

**Web UI** (`epilogue_server.py`): since epilogue is instant, the preview
re-renders live as you flip toggles. Drop an SVG and every option is a
tooltip-annotated toggle/field; a before/after switch compares the raw upload
against the flattened result, and a colour audit panel lists each op colour,
snap remaps, and reserved-black warnings.

```bash
python epilogue_server.py           # -> http://127.0.0.1:5003
PORT=8080 python epilogue_server.py
```

## Launch every web UI with one command

`toolz.py` boots all three Flask frontends together — each stays on its own
port, its logs prefixed by tool name, and a single Ctrl-C (or `kill`) tears the
whole set down. If one server can't start (missing dep, port in use) the others
keep serving. Every page also grows a small nav in its header linking the other
two, so you can switch tools on demand; the links track any port overrides (and
still work when a server is launched on its own, falling back to the defaults).

```bash
python toolz.py                     # linify :5001 · segment :5002 · epilogue :5003
python toolz.py segment epilogue    # just a subset (any order)
python toolz.py linify=8080         # override a port
python toolz.py --list              # show the tools and exit
```

## Files

- `linify.py` — the CLI (and importable rendering API: `image_to_svg(src, Params)`).
- `server.py` — the optional local web UI for linify.
- `segment.py` — the segmentation-mask CLI (MobileSAM → filled region SVG).
- `segment_server.py` — the interactive click-to-pick web UI for segment.
- `epilogue.py` — the Epilog-safe SVG preflight CLI (`svg_to_epilog(src, EpiParams)`).
- `epilogue_server.py` — the live-preview web UI for epilogue.
- `toolz.py` — one-command launcher that boots all three web UIs together.
- `toolz_nav.py` — the shared header nav that lets the three UIs link to each other.
- `sample.png` — a synthetic test portrait used by the examples above.
```
