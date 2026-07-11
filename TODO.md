# TODO — future image → SVG tools

Ideas for additional tools/modes. Everything must obey the core rule: **the
laser ignores stroke width, so tone is encoded by geometry only** (displacement,
spacing, density, contour) — never by line thickness or color. Output stays as
continuous hairline `<path>` polylines with real physical units.

## Tone-via-geometry raster fills (siblings to `wavy` / `spacing` / `contour`)

- **`hatch` — cross-hatching by tone.** Darkness selects how many overlaid line
  families fire (0 → none, 1 → one diagonal set, 2 → cross, 3 → +vertical,
  4 → +horizontal). The classic engraver's tonal ladder; pure geometry. Params:
  angle, base spacing.
- **`stipple` / TSP-art.** Weighted Voronoi (Lloyd's relaxation) stippling packs
  more dots in dark areas, then connect them into a single continuous path via a
  nearest-neighbor / TSP tour — one unbroken line that reads as a full tonal
  portrait. Very on-brand for laser perforation.
- **`spiral` — modulated Archimedean spiral.** One spiral from center outward;
  darkness modulates its radial wiggle amplitude (single-line spiral portrait).
  Naturally a single continuous path.
- **`concentric` / `wavy-radial`.** The `wavy` idea with circular or radial
  carriers instead of horizontal scanlines — for portraits/logos where flow
  shouldn't be horizontal.

## Structure-following (edges / flow rather than fills)

- **`flowfield` — streamlines along image gradient.** Structure tensor / gradient
  field, seed particles, integrate streamlines that follow the form (hair,
  fabric, terrain). Seeding density from darkness. Organic and mathematically
  clean.
- **`edges` — Canny/DoG contour tracing.** Extract edges, vectorize into
  polylines (line-drawing, not tone). XDoG / Gaussian-pyramid coherence for a
  hand-inked look. Complements `contour` (iso-brightness, not edges).
- **`centerline` — skeletonization.** Threshold → medial-axis skeleton → trace.
  For high-contrast / logo / text art: true single-stroke centerlines (score the
  line once) instead of double outlines.

## Pattern / halftone geometry

- **`halftone-line` — wave halftone.** Sinusoidal lines whose local *frequency*
  encodes tone (musical-score / ripple look). Distinct from `wavy`, where
  amplitude is the primary channel.
- **`truchet` — Truchet tiles.** Tile the image; darkness picks which arc/line
  tile variant to place. Mazy continuous curves; tile seams connect into flowing
  paths.
- **`maze` / `spacefill` — Hilbert/Peano space-filling curve.** Recurse deeper in
  dark regions (variable subdivision); a single continuous fractal line whose
  local density = tone. Literally one unbroken path.

## Utility tools (SVG manipulation)

- **`perforate` — dash/stitch post-processor.** Convert continuous paths into
  on/off stitch patterns (by length or by underlying darkness) natively, instead
  of only in LightBurn downstream.
- **`optimize` — path ordering / join.** Merge collinear segments, join endpoints
  within tolerance into longer polylines, reorder paths to minimize laser travel
  (greedy nearest-neighbor). Cutting-time and file-size win for every mode.

## Suggested first picks

Highest payoff-to-effort and most true to the "single continuous stroke" spirit:

1. **`hatch`** — cleanest fit next to the existing scanline modes.
2. **`spiral`** — most striking output, naturally one path.
3. **`stipple` + TSP** — full tonal portrait as a single line.
