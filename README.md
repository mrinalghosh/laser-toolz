# linify — image → laser-ready SVG line art

Convert any raster image into **continuous hairline SVG lines** for laser
cutting / perforation. Three interchangeable render modes, one parametric CLI,
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
pip install pillow numpy scikit-image   # scikit-image is only needed for contour mode
pip install flask                        # only for the optional web UI
```

## Quick start

```bash
python linify.py sample.png -o out.svg --mode wavy      # zero tuning needed
```

## The three modes

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

### 2. `spacing` — density lines

Horizontal lines that **pack closer in dark regions** and spread apart in light
ones. Implemented by walking top→bottom, accumulating a row-darkness integral,
and emitting a line each time it crosses a threshold.

> Honest tradeoff: this mode carries **vertical** tonal detail well but is
> **coarse horizontally** (each line is a full-width horizontal). That's
> expected — reach for `contour` if you need form.

```bash
python linify.py sample.png -o spacing.svg --mode spacing \
    --min-spacing 0.6 --max-spacing 4
```

### 3. `contour` — topographic iso-lines

Quantizes brightness into `--levels` bands and traces each iso-brightness
contour with `skimage.measure.find_contours`. Organic, follows the form (the
displacement-map look).

```bash
python linify.py sample.png -o contour.svg --mode contour \
    --levels 8 --smooth 1.5 --min-contour-len 3
```

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
| `--mode` | `wavy` | all | `wavy` \| `spacing` \| `contour` |
| `-o, --output` | stdout | all | output SVG path |
| `--width-mm` | `200` | all | physical output width in mm (height from aspect) |
| `--units` | `mm` | all | header unit: `mm` \| `cm` \| `in` (`--width-mm` stays mm) |
| `--stroke-width` | `0.02` | all | hairline width in mm (tonally irrelevant) |
| `--color` | `black` | all | single stroke color (`red` = cut layer) |
| `--invert` | off | all | flip dark↔light tonal encoding |
| `--mask-threshold` | none | all | skip where brightness > T (erase background) |
| `--samples` | `800` | all | point density sampled along each line |
| `--decimate` | `0.03` | all | collinear-point removal tolerance (mm) — smaller = more points |
| `--resample` | `900` | all | working image resolution, longest edge (px) |
| `--line-spacing` | `2` | wavy | mm between scanline baselines (sets line count) |
| `--amp` | spacing/2 | wavy | max wiggle amplitude mm (auto-clamped < spacing/2) |
| `--wavelength` | `8` | wavy | carrier wavelength in mm |
| `--freq-mod` | off | wavy | also raise spatial frequency in dark regions |
| `--freq-amount` | `1.0` | wavy | strength of `--freq-mod` (0..~2) |
| `--min-spacing` | `0.6` | spacing | mm between lines in the darkest regions |
| `--max-spacing` | `4` | spacing | mm between lines in the lightest regions |
| `--levels` | `8` | contour | number of brightness bands |
| `--smooth` | `0` | contour | Gaussian blur sigma pre-contour (px), try 1–2 |
| `--min-contour-len` | `2` | contour | drop contours shorter than this (mm) |

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
  (e.g. `portrait_wavy.svg`), not a generic name.
- **Advanced panel** — exposes the finer knobs (`samples`, `resample`,
  `decimate`, `stroke-width`, `freq-amount`) for granular tuning.

```bash
pip install flask
python server.py                 # -> http://127.0.0.1:5001
PORT=8080 python server.py       # override the port if you like
```

> On macOS, port 5000 is taken by the AirPlay Receiver (it returns 403 and looks
> like a crash), so the server defaults to **5001**.

## Files

- `linify.py` — the CLI (and importable rendering API: `image_to_svg(src, Params)`).
- `server.py` — the optional local web UI.
- `sample.png` — a synthetic test portrait used by the examples above.
```
